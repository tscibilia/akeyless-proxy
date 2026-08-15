# tests/test_client.py
import json
import unittest
from io import BytesIO

from proxy import AKeylessClient


class FakeResponse(BytesIO):
    """Minimal stand-in for the object urlopen returns as a context manager."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Records requests and replays queued JSON payloads."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.requests = []

    def __call__(self, req):
        self.requests.append(req)
        if not self.payloads:
            raise AssertionError("FakeOpener ran out of queued payloads")
        return FakeResponse(json.dumps(self.payloads.pop(0)).encode())


class TestGetToken(unittest.TestCase):
    def test_posts_api_key_credentials_and_returns_token(self):
        opener = FakeOpener([{"token": "t-abc"}])
        client = AKeylessClient("p-id", "key", opener=opener)

        self.assertEqual(client.get_token(), "t-abc")

        self.assertEqual(len(opener.requests), 1)
        req = opener.requests[0]
        self.assertEqual(req.full_url, "https://api.akeyless.io/auth")
        self.assertEqual(req.get_method(), "POST")
        body = json.loads(req.data)
        self.assertEqual(body["access-id"], "p-id")
        self.assertEqual(body["access-key"], "key")
        self.assertEqual(body["access-type"], "access_key")

    def test_raises_when_response_has_no_token(self):
        client = AKeylessClient("p-id", "key", opener=FakeOpener([{}]))
        with self.assertRaises(ValueError):
            client.get_token()


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestTokenCache(unittest.TestCase):
    def test_second_call_reuses_the_cached_token(self):
        opener = FakeOpener([{"token": "t-1"}, {"token": "t-2"}])
        client = AKeylessClient("p-id", "key", opener=opener, clock=FakeClock())

        self.assertEqual(client.get_token(), "t-1")
        self.assertEqual(client.get_token(), "t-1")
        self.assertEqual(len(opener.requests), 1)

    def test_token_is_refetched_after_ttl(self):
        clock = FakeClock()
        opener = FakeOpener([{"token": "t-1"}, {"token": "t-2"}])
        client = AKeylessClient(
            "p-id", "key", token_ttl=3000, opener=opener, clock=clock
        )

        self.assertEqual(client.get_token(), "t-1")
        clock.advance(3001)
        self.assertEqual(client.get_token(), "t-2")
        self.assertEqual(len(opener.requests), 2)

    def test_concurrent_callers_mint_one_token(self):
        import threading as _t

        opener = FakeOpener([{"token": "t-1"}] + [{"token": "extra"}] * 8)
        client = AKeylessClient("p-id", "key", opener=opener, clock=FakeClock())

        start = _t.Barrier(8)
        seen = []

        def worker():
            start.wait()
            seen.append(client.get_token())

        threads = [_t.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(set(seen), {"t-1"})
        self.assertEqual(len(opener.requests), 1)


class TestGetSecret(unittest.TestCase):
    def test_posts_name_and_token_then_returns_body_unmodified(self):
        opener = FakeOpener([{"token": "t-1"}, {"/a/b": "value"}])
        client = AKeylessClient("p-id", "key", opener=opener, clock=FakeClock())

        self.assertEqual(client.get_secret("/a/b"), {"/a/b": "value"})

        secret_req = opener.requests[1]
        self.assertEqual(
            secret_req.full_url, "https://api.akeyless.io/get-secret-value"
        )
        body = json.loads(secret_req.data)
        self.assertEqual(body["names"], ["/a/b"])
        self.assertEqual(body["token"], "t-1")

    def test_repeat_name_is_served_from_cache(self):
        opener = FakeOpener([{"token": "t-1"}, {"/a/b": "v"}, {"/a/b": "other"}])
        client = AKeylessClient("p-id", "key", opener=opener, clock=FakeClock())

        self.assertEqual(client.get_secret("/a/b"), {"/a/b": "v"})
        self.assertEqual(client.get_secret("/a/b"), {"/a/b": "v"})
        self.assertEqual(len(opener.requests), 2)  # 1 auth + 1 secret

    def test_secret_is_refetched_after_ttl(self):
        clock = FakeClock()
        opener = FakeOpener([{"token": "t-1"}, {"/a/b": "v"}, {"/a/b": "v2"}])
        client = AKeylessClient(
            "p-id", "key", secret_ttl=300, opener=opener, clock=clock
        )

        self.assertEqual(client.get_secret("/a/b"), {"/a/b": "v"})
        clock.advance(301)
        self.assertEqual(client.get_secret("/a/b"), {"/a/b": "v2"})

    def test_distinct_names_are_cached_separately(self):
        opener = FakeOpener([{"token": "t-1"}, {"/a": "1"}, {"/b": "2"}])
        client = AKeylessClient("p-id", "key", opener=opener, clock=FakeClock())

        self.assertEqual(client.get_secret("/a"), {"/a": "1"})
        self.assertEqual(client.get_secret("/b"), {"/b": "2"})
        self.assertEqual(client.get_secret("/a"), {"/a": "1"})
        self.assertEqual(len(opener.requests), 3)


if __name__ == "__main__":
    unittest.main()
