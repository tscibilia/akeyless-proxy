import json
import threading
import unittest
import urllib.error
import urllib.request

from proxy import ProxyServer, make_handler


class StubClient:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else {"/a": "v"}
        self.error = error
        self.calls = []

    def get_secret(self, name):
        self.calls.append(name)
        if self.error:
            raise self.error
        return self.result


class ServerHarness:
    """Runs the real handler on an ephemeral port."""

    def __init__(self, client, keepalive=False):
        self.server = ProxyServer(("127.0.0.1", 0), make_handler(client, keepalive))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class TestHandler(unittest.TestCase):
    def make(self, client, keepalive=False):
        harness = ServerHarness(client, keepalive)
        self.addCleanup(harness.close)
        return harness

    def test_returns_the_client_body_unmodified(self):
        h = self.make(StubClient({"/a/b": "secret"}))
        with urllib.request.urlopen(h.url("/secret?name=/a/b")) as r:
            self.assertEqual(r.status, 200)
            self.assertEqual(json.loads(r.read()), {"/a/b": "secret"})

    def test_url_encoded_name_is_decoded(self):
        client = StubClient()
        h = self.make(client)
        urllib.request.urlopen(h.url("/secret?name=%2Fa%2Fb")).close()
        self.assertEqual(client.calls, ["/a/b"])

    def test_missing_name_is_400(self):
        h = self.make(StubClient())
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(h.url("/secret"))
        self.assertEqual(cm.exception.code, 400)
        cm.exception.close()

    def test_unknown_path_is_404(self):
        h = self.make(StubClient())
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(h.url("/nope"))
        self.assertEqual(cm.exception.code, 404)
        cm.exception.close()

    def test_upstream_http_error_status_is_passed_through(self):
        err = urllib.error.HTTPError(
            "u", 403, "Forbidden", {}, __import__("io").BytesIO(b'{"error":"denied"}')
        )
        h = self.make(StubClient(error=err))
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(h.url("/secret?name=/a"))
        self.assertEqual(cm.exception.code, 403)
        cm.exception.close()

    def test_unexpected_error_is_500(self):
        h = self.make(StubClient(error=RuntimeError("boom")))
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(h.url("/secret?name=/a"))
        self.assertEqual(cm.exception.code, 500)
        cm.exception.close()

    def test_content_length_is_always_present(self):
        h = self.make(StubClient({"/a": "v"}))
        with urllib.request.urlopen(h.url("/secret?name=/a")) as r:
            self.assertIsNotNone(r.headers["Content-Length"])


class TestKeepAlive(unittest.TestCase):
    def test_default_is_http_1_0_and_closes(self):
        handler = make_handler(StubClient(), keepalive=False)
        self.assertEqual(handler.protocol_version, "HTTP/1.0")
        self.assertIsNone(handler.timeout)

    def test_opt_in_selects_http_1_1_with_idle_timeout(self):
        handler = make_handler(StubClient(), keepalive=True)
        self.assertEqual(handler.protocol_version, "HTTP/1.1")
        self.assertEqual(handler.timeout, 30)

    def test_keepalive_server_advertises_a_persistent_connection(self):
        harness = ServerHarness(StubClient({"/a": "v"}), keepalive=True)
        self.addCleanup(harness.close)
        with urllib.request.urlopen(harness.url("/secret?name=/a")) as r:
            self.assertEqual(r.version, 11)
            self.assertNotEqual(
                (r.headers["Connection"] or "").lower(), "close"
            )

    def test_default_server_reports_http_1_0(self):
        harness = ServerHarness(StubClient({"/a": "v"}), keepalive=False)
        self.addCleanup(harness.close)
        with urllib.request.urlopen(harness.url("/secret?name=/a")) as r:
            self.assertEqual(r.version, 10)


if __name__ == "__main__":
    unittest.main()
