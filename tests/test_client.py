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


if __name__ == "__main__":
    unittest.main()
