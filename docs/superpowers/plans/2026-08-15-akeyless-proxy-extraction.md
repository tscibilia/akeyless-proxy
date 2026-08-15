# akeyless-proxy Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace three drifted hand-copied `proxy.py` files in `tscibilia/home-ops` with one tested container image published from this repository and consumed by pinned digest.

**Architecture:** A stdlib-only Python HTTP server. Transport logic (`AKeylessClient`) is separated from the HTTP surface (`make_handler`) so the two-step aKeyless flow and its caches are unit-testable with an injected opener, and the handler is tested against a real socket on an ephemeral port. Keep-alive is a constructor flag, not a global, because two of the three consumers must not have it.

**Tech Stack:** Python 3.14 (stdlib only — `http.server`, `urllib.request`, `threading`, `unittest`), Alpine container, GitHub Actions, ghcr.io.

**Spec:** `docs/adr/0001-http-adapter-over-doco-cd-configuration.md`, `docs/adr/0002-standalone-repo-publishing-an-image.md`, `docs/adr/0003-keep-alive-is-opt-in-per-host.md`, `docs/adr/0004-public-package-consumers-pin-by-digest.md`, plus `tscibilia/home-ops` issue #3367.

## Global Constraints

- **No third-party dependencies.** Standard library only. No `requirements.txt`, no `pip install` in the `Dockerfile`.
- **Python 3.14** — matches `python:3.14-alpine`, the base image already in use.
- **No network in tests.** Every test injects a fake opener. A test that reaches `api.akeyless.io` is a failing test.
- **Credentials are never baked into the image.** Read at startup from `AKEYLESS_ACCESS_ID` / `AKEYLESS_ACCESS_KEY` or their `*_FILE` variants.
- **`Content-Length` on every response path.** Mandatory under HTTP/1.1, harmless under HTTP/1.0, so the send path stays branch-free.
- **`PROXY_KEEPALIVE` defaults to off.** Per ADR-0003.
- **Response bodies are returned unmodified.** Consumers own the JMESPath.
- **Token TTL 3000s (50 min); secret TTL 300s (5 min).** Per README.
- Repository is public. No internal hostnames, aKeyless paths, or credentials in any committed file.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `proxy.py` | Everything: `AKeylessClient`, `make_handler`, `main`. One file, ~180 lines. Kept flat because the `Dockerfile` copies exactly one source file (ADR-0002). |
| `tests/test_client.py` | `AKeylessClient` — auth, caching, TTL expiry, thread safety. Fake opener. |
| `tests/test_handler.py` | HTTP surface — routing, status codes, error passthrough, keep-alive header. Real socket, fake client. |
| `Dockerfile` | `python:3.14-alpine`, copy `proxy.py`, healthcheck, entrypoint. |
| `.github/workflows/build.yaml` | Build and push to ghcr.io on tag; run tests on every push and PR. |
| `.gitignore` | Already present. Add `__pycache__/` if absent. |

Consumer-side changes land in `tscibilia/home-ops` and are Phase 2.

---

## Phase 1 — This repository

### Task 1: Test scaffolding and the auth call

**Files:**
- Create: `proxy.py`
- Create: `tests/__init__.py` (empty)
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `AKeylessClient(access_id: str, access_key: str, api_url: str = "https://api.akeyless.io", token_ttl: float = 3000, secret_ttl: float = 300, opener: Callable = urllib.request.urlopen, clock: Callable[[], float] = time.monotonic)`, with method `get_token() -> str`.

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `python3 -m unittest tests.test_client -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'proxy'`

- [ ] **Step 3: Write the minimal implementation**

```python
#!/usr/bin/env python3
"""
Minimal aKeyless auth proxy for doco-cd's webhook secret provider.

doco-cd issues one HTTP request per secret with no auth chaining. aKeyless
needs a token from POST /auth before POST /get-secret-value will answer.
This bridges the two. See docs/adr/0001.
"""

import json
import threading
import time
import urllib.request


class AKeylessClient:
    """Two-step aKeyless access with a cached token."""

    def __init__(
        self,
        access_id,
        access_key,
        api_url="https://api.akeyless.io",
        token_ttl=3000,
        secret_ttl=300,
        opener=urllib.request.urlopen,
        clock=time.monotonic,
    ):
        self._access_id = access_id
        self._access_key = access_key
        self._api_url = api_url.rstrip("/")
        self._token_ttl = token_ttl
        self._secret_ttl = secret_ttl
        self._open = opener
        self._clock = clock

        self._token = None
        self._token_expiry = 0.0
        self._token_lock = threading.Lock()

    def _post(self, path, payload):
        req = urllib.request.Request(
            f"{self._api_url}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self._open(req) as resp:
            return json.loads(resp.read())

    def get_token(self):
        data = self._post(
            "/auth",
            {
                "access-id": self._access_id,
                "access-key": self._access_key,
                "access-type": "access_key",
            },
        )
        token = data.get("token")
        if not token:
            raise ValueError("no token in aKeyless auth response")
        return token
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `python3 -m unittest tests.test_client -v`
Expected: PASS, 2 tests

- [ ] **Step 5: Commit**

```bash
git add proxy.py tests/__init__.py tests/test_client.py
git commit -m "feat: aKeyless auth call with injectable opener"
```

---

### Task 2: Token caching with TTL

**Files:**
- Modify: `proxy.py` — `AKeylessClient.get_token`
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: `AKeylessClient` from Task 1.
- Produces: `get_token()` now returns a cached token until `token_ttl` elapses on the injected `clock`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_client.py`:

```python
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
```

- [ ] **Step 2: Run and confirm failure**

Run: `python3 -m unittest tests.test_client -v`
Expected: FAIL — `test_second_call_reuses_the_cached_token` sees 2 requests

- [ ] **Step 3: Implement the cache**

Replace `get_token` in `proxy.py`:

```python
    def get_token(self):
        with self._token_lock:
            if self._token and self._clock() < self._token_expiry:
                return self._token

            data = self._post(
                "/auth",
                {
                    "access-id": self._access_id,
                    "access-key": self._access_key,
                    "access-type": "access_key",
                },
            )
            token = data.get("token")
            if not token:
                raise ValueError("no token in aKeyless auth response")

            self._token = token
            self._token_expiry = self._clock() + self._token_ttl
            return token
```

- [ ] **Step 4: Run and confirm pass**

Run: `python3 -m unittest tests.test_client -v`
Expected: PASS, 5 tests

- [ ] **Step 5: Commit**

```bash
git add proxy.py tests/test_client.py
git commit -m "feat: cache the aKeyless token for 50 minutes"
```

---

### Task 3: Secret fetch with its own cache

**Files:**
- Modify: `proxy.py` — add `get_secret`
- Test: `tests/test_client.py`

**Interfaces:**
- Consumes: `get_token()` from Task 2.
- Produces: `get_secret(name: str) -> dict` — the aKeyless response body, unmodified, cached for `secret_ttl`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_client.py`:

```python
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
```

- [ ] **Step 2: Run and confirm failure**

Run: `python3 -m unittest tests.test_client -v`
Expected: FAIL — `AttributeError: 'AKeylessClient' object has no attribute 'get_secret'`

- [ ] **Step 3: Implement**

Add to `__init__`, after the token fields:

```python
        self._secrets = {}
        self._secret_lock = threading.Lock()
```

Add the method:

```python
    def get_secret(self, name):
        with self._secret_lock:
            entry = self._secrets.get(name)
            if entry and self._clock() < entry[1]:
                return entry[0]

        # Deliberately outside the lock: a slow upstream call must not block
        # lookups of other names. A duplicate in-flight fetch is cheaper than
        # serialising every deploy through one mutex.
        data = self._post(
            "/get-secret-value", {"names": [name], "token": self.get_token()}
        )

        with self._secret_lock:
            self._secrets[name] = (data, self._clock() + self._secret_ttl)
        return data
```

- [ ] **Step 4: Run and confirm pass**

Run: `python3 -m unittest tests.test_client -v`
Expected: PASS, 9 tests

- [ ] **Step 5: Commit**

```bash
git add proxy.py tests/test_client.py
git commit -m "feat: fetch and cache secret values for 5 minutes"
```

---

### Task 4: HTTP surface

**Files:**
- Modify: `proxy.py` — add `make_handler`, `ProxyServer`
- Test: `tests/test_handler.py`

**Interfaces:**
- Consumes: `AKeylessClient` (only its `get_secret` method is used, so tests pass a stub).
- Produces: `make_handler(client, keepalive: bool = False) -> type[BaseHTTPRequestHandler]` and `ProxyServer(addr, handler_cls)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_handler.py
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

    def test_unknown_path_is_404(self):
        h = self.make(StubClient())
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(h.url("/nope"))
        self.assertEqual(cm.exception.code, 404)

    def test_upstream_http_error_status_is_passed_through(self):
        err = urllib.error.HTTPError(
            "u", 403, "Forbidden", {}, __import__("io").BytesIO(b'{"error":"denied"}')
        )
        h = self.make(StubClient(error=err))
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(h.url("/secret?name=/a"))
        self.assertEqual(cm.exception.code, 403)

    def test_unexpected_error_is_500(self):
        h = self.make(StubClient(error=RuntimeError("boom")))
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(h.url("/secret?name=/a"))
        self.assertEqual(cm.exception.code, 500)

    def test_content_length_is_always_present(self):
        h = self.make(StubClient({"/a": "v"}))
        with urllib.request.urlopen(h.url("/secret?name=/a")) as r:
            self.assertIsNotNone(r.headers["Content-Length"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run and confirm failure**

Run: `python3 -m unittest tests.test_handler -v`
Expected: FAIL — `ImportError: cannot import name 'ProxyServer' from 'proxy'`

- [ ] **Step 3: Implement**

Add imports to `proxy.py`:

```python
import sys
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
```

Add below `AKeylessClient`:

```python
class ProxyServer(ThreadingHTTPServer):
    # Python's default listen backlog is 5. doco-cd bursts 20-40 secret
    # fetches in a second during a deploy, overflowing the accept queue so
    # the kernel RSTs the excess and the deploy fails on "connection reset
    # by peer". Measured: ListenOverflows=5667 in 10.5h. See docs/adr/0003.
    request_queue_size = 128
    daemon_threads = True
    allow_reuse_address = True


def make_handler(client, keepalive=False):
    """Build a handler bound to `client`. keepalive per docs/adr/0003."""

    class AKeylessProxyHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1" if keepalive else "HTTP/1.0"
        timeout = 30 if keepalive else None

        def _send(self, code, body):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != "/secret":
                self._send(404, b'{"error": "not found"}')
                return

            name = parse_qs(parsed.query).get("name", [None])[0]
            if not name:
                self._send(400, b'{"error": "missing name query parameter"}')
                return

            try:
                self._send(200, json.dumps(client.get_secret(name)).encode())
            except urllib.error.HTTPError as e:
                self._send(e.code, e.read())
            except Exception as e:  # noqa: BLE001 - surface any failure as 500
                self._send(500, json.dumps({"error": str(e)}).encode())

        def log_message(self, fmt, *args):
            sys.stdout.write(f"[{self.log_date_time_string()}] {fmt % args}\n")
            sys.stdout.flush()

    return AKeylessProxyHandler
```

- [ ] **Step 4: Run and confirm pass**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS, 16 tests

- [ ] **Step 5: Commit**

```bash
git add proxy.py tests/test_handler.py
git commit -m "feat: HTTP surface with error passthrough"
```

---

### Task 5: Keep-alive toggle proven end to end

**Files:**
- Test: `tests/test_handler.py`

**Interfaces:**
- Consumes: `make_handler(client, keepalive)` from Task 4. No production change expected — this task proves the flag and fails the build if a future edit hard-codes the protocol.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_handler.py`:

```python
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
```

- [ ] **Step 2: Run and confirm the suite is green or red for the right reason**

Run: `python3 -m unittest tests.test_handler -v`
Expected: PASS, 11 tests. If any fail, Task 4's `make_handler` hard-coded the protocol — fix `make_handler`, not the test.

- [ ] **Step 3: Commit**

```bash
git add tests/test_handler.py
git commit -m "test: pin keep-alive default off and opt-in behaviour"
```

---

### Task 6: Startup, credential loading, entrypoint

**Files:**
- Modify: `proxy.py` — add `read_credential`, `main`
- Test: `tests/test_startup.py`

**Interfaces:**
- Consumes: `AKeylessClient`, `make_handler`, `ProxyServer`.
- Produces: `read_credential(env, name) -> str | None` and `main(argv=None, env=None) -> int`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_startup.py
import tempfile
import unittest
from pathlib import Path

from proxy import read_credential


class TestReadCredential(unittest.TestCase):
    def test_direct_env_var_wins(self):
        env = {"X": "direct", "X_FILE": "/nonexistent"}
        self.assertEqual(read_credential(env, "X"), "direct")

    def test_file_variant_is_read_and_stripped(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "secret"
            p.write_text("  from-file\n")
            self.assertEqual(
                read_credential({"X_FILE": str(p)}, "X"), "from-file"
            )

    def test_returns_none_when_neither_is_set(self):
        self.assertIsNone(read_credential({}, "X"))


class TestMain(unittest.TestCase):
    def test_exits_non_zero_without_credentials(self):
        from proxy import main

        self.assertNotEqual(main(env={}), 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run and confirm failure**

Run: `python3 -m unittest tests.test_startup -v`
Expected: FAIL — `ImportError: cannot import name 'read_credential'`

- [ ] **Step 3: Implement**

Add `import os` to the imports, then append to `proxy.py`:

```python
def read_credential(env, name):
    """Value of `name`, else the contents of the path in `name`_FILE."""
    direct = env.get(name)
    if direct:
        return direct
    path = env.get(f"{name}_FILE")
    if path:
        with open(path) as fh:
            return fh.read().strip()
    return None


def main(argv=None, env=None):
    env = os.environ if env is None else env

    access_id = read_credential(env, "AKEYLESS_ACCESS_ID")
    access_key = read_credential(env, "AKEYLESS_ACCESS_KEY")
    if not access_id or not access_key:
        print(
            "error: AKEYLESS_ACCESS_ID and AKEYLESS_ACCESS_KEY must be set "
            "(or their _FILE variants)",
            file=sys.stderr,
        )
        return 1

    keepalive = env.get("PROXY_KEEPALIVE", "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )

    client = AKeylessClient(access_id, access_key)
    server = ProxyServer(("0.0.0.0", 8080), make_handler(client, keepalive))
    print(
        f"aKeyless proxy listening on http://0.0.0.0:8080 "
        f"(keepalive={'on' if keepalive else 'off'})",
        file=sys.stderr,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the whole suite**

Run: `python3 -m unittest discover -s tests -v`
Expected: PASS, 24 tests

- [ ] **Step 5: Commit**

```bash
git add proxy.py tests/test_startup.py
git commit -m "feat: credential loading and entrypoint"
```

---

### Task 7: Container image

**Files:**
- Create: `Dockerfile`
- Create: `.dockerignore`

**Interfaces:**
- Consumes: `proxy.py` with `main()` from Task 6.
- Produces: an image exposing port 8080 with a working healthcheck.

- [ ] **Step 1: Write the Dockerfile**

```dockerfile
FROM python:3.14-alpine

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY proxy.py .

# Probes the listener only. A deeper check would need credentials and would
# spend an aKeyless call every interval.
HEALTHCHECK --interval=5s --timeout=3s --start-period=2s --retries=3 \
  CMD python3 -c "import socket; socket.create_connection(('localhost', 8080), timeout=2)" || exit 1

USER nobody

ENTRYPOINT ["python3", "proxy.py"]
```

- [ ] **Step 2: Write .dockerignore**

```text
.git
.github
.agents
.opencode
docs
tests
README.md
AGENTS.md
CLAUDE.md
__pycache__
```

- [ ] **Step 3: Build and verify it refuses to start without credentials**

```bash
docker build -t akeyless-proxy:dev .
docker run --rm akeyless-proxy:dev; echo "exit=$?"
```

Expected: the credential error on stderr and `exit=1`.

- [ ] **Step 4: Verify it serves with credentials**

```bash
docker run --rm -d --name ap-test -p 18080:8080 \
  -e AKEYLESS_ACCESS_ID=p-fake -e AKEYLESS_ACCESS_KEY=fake akeyless-proxy:dev
sleep 2
curl -s -o /dev/null -w '%{http_code}\n' localhost:18080/nope      # expect 404
curl -s -o /dev/null -w '%{http_code}\n' localhost:18080/secret    # expect 400
docker inspect --format '{{.State.Health.Status}}' ap-test          # expect healthy
docker rm -f ap-test
```

- [ ] **Step 5: Commit**

```bash
git add Dockerfile .dockerignore
git commit -m "feat: container image"
```

---

### Task 8: CI — test on every push, publish on tag

**Files:**
- Create: `.github/workflows/build.yaml`

**Interfaces:**
- Consumes: `tests/`, `Dockerfile`.
- Produces: `ghcr.io/tscibilia/akeyless-proxy` tagged from the git tag, plus a digest in the job summary.

- [ ] **Step 1: Write the workflow**

```yaml
---
# yaml-language-server: $schema=https://json.schemastore.org/github-workflow.json
name: Build

on:
  push:
    branches: ["main"]
    tags: ["v*"]
  pull_request:
    branches: ["main"]

permissions:
  contents: read

jobs:
  test:
    name: Test
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v5
        with:
          persist-credentials: false

      - name: Setup Python
        uses: actions/setup-python@v6
        with:
          python-version: "3.14"

      - name: Run tests
        run: python3 -m unittest discover -s tests -v

  publish:
    name: Publish
    needs: test
    if: startsWith(github.ref, 'refs/tags/v')
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - name: Checkout
        uses: actions/checkout@v5
        with:
          persist-credentials: false

      - name: Log in to ghcr.io
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Derive metadata
        id: meta
        uses: docker/metadata-action@v5
        with:
          images: ghcr.io/${{ github.repository }}
          tags: |
            type=semver,pattern={{version}}
            type=semver,pattern={{major}}.{{minor}}

      - name: Build and push
        id: push
        uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}

      - name: Report digest
        run: |
          echo "### Pin this digest in consumers" >> "$GITHUB_STEP_SUMMARY"
          echo '```yaml' >> "$GITHUB_STEP_SUMMARY"
          echo "image: ghcr.io/${{ github.repository }}:${{ github.ref_name }}@${{ steps.push.outputs.digest }}" >> "$GITHUB_STEP_SUMMARY"
          echo '```' >> "$GITHUB_STEP_SUMMARY"
```

> Action versions are floating majors here. Pin them to commit SHAs to match the convention in `tscibilia/home-ops` before the first release.

- [ ] **Step 2: Commit and push, confirm the test job runs**

```bash
git add .github/workflows/build.yaml
git commit -m "ci: test on push, publish image on tag"
git push -u origin main
```

Expected: the `Test` job passes; `Publish` is skipped because this is not a tag.

- [ ] **Step 3: Verify the publish job is correctly gated**

Run: `gh run list --workflow build.yaml --limit 1`
Expected: one run, `Publish` skipped.

---

### Task 9: Release v1.0.0

**Files:** none — this task produces an artefact, not a commit.

**Interfaces:**
- Consumes: everything above.
- Produces: `ghcr.io/tscibilia/akeyless-proxy:1.0.0@sha256:<digest>`, the string Phase 2 pins.

- [ ] **Step 1: Create the GitHub repository and push** *(human step — needs an authenticated account)*

```bash
gh repo create tscibilia/akeyless-proxy --public --source=. --remote=origin --push
```

- [ ] **Step 2: Tag and push the tag**

```bash
git tag -a v1.0.0 -m "Initial release: extracted from home-ops"
git push origin v1.0.0
```

- [ ] **Step 3: Wait for the publish job and capture the digest**

```bash
gh run watch
gh run view --log | grep -A3 'Pin this digest'
```

Record the `sha256:` value. Every Phase 2 task needs it.

- [ ] **Step 4: Make the package public**

GitHub → Packages → `akeyless-proxy` → Package settings → Change visibility → Public.

Verify anonymously:

```bash
docker logout ghcr.io
docker pull ghcr.io/tscibilia/akeyless-proxy:1.0.0
```

Expected: pull succeeds with no credentials. Per ADR-0004.

---

## Phase 2 — `tscibilia/home-ops`

> Work in `/home/tscibilia/home-ops` on a branch. `<DIGEST>` below is the value captured in Task 9 Step 3.
>
> **Safety:** every task here ends with a change to a live host. Per `home-ops/AGENTS.md`, anything touching a live host is ask-first. Do not run `cron-update.sh` on a host without checking in.

### Task 10: Adopt the image on `vps`

**Files:**
- Modify: `docker/vps/.doco-cd/docker-compose.app.yaml`
- Modify: `docker/vps/.doco-cd/cron-update.sh`
- Modify: `ansible/vps/playbook.yaml`
- Delete: `docker/vps/.doco-cd/proxy.py`, `docker/vps/.doco-cd/Dockerfile`

**Interfaces:**
- Consumes: the published digest from Task 9.
- Produces: a `vps` host running the published image with keep-alive on.

- [ ] **Step 1: Swap the build for a pinned image**

In `docker/vps/.doco-cd/docker-compose.app.yaml`, replace the `akeyless-proxy` service's build block:

```yaml
  akeyless-proxy:
    image: ghcr.io/tscibilia/akeyless-proxy:1.0.0@sha256:<DIGEST>
    container_name: akeyless-proxy
    restart: unless-stopped
    expose:
      - "8080"
    environment:
      AKEYLESS_ACCESS_ID_FILE: /run/secrets/akeyless_access_id
      AKEYLESS_ACCESS_KEY_FILE: /run/secrets/akeyless_access_key
      # 21 secrets per deploy here; without keep-alive the connection burst
      # overflows the accept queue. See akeyless-proxy docs/adr/0003.
      PROXY_KEEPALIVE: "1"
    secrets:
      - akeyless_access_id
      - akeyless_access_key
```

- [ ] **Step 2: Stop the self-updater fetching build inputs**

In `docker/vps/.doco-cd/cron-update.sh`, delete these two lines:

```bash
fetch_and_compare "Dockerfile"
fetch_and_compare "proxy.py"
```

and change the rebuild command from:

```bash
  docker compose -f "$WORK_DIR/docker-compose.app.yaml" up -d --build --force-recreate
```

to:

```bash
  docker compose -f "$WORK_DIR/docker-compose.app.yaml" up -d --pull always --force-recreate
```

- [ ] **Step 3: Stop Ansible copying deleted files**

In `ansible/vps/playbook.yaml`, delete the two tasks named `doco-cd | Copy Dockerfile` and `doco-cd | Copy proxy.py`.

- [ ] **Step 4: Delete the local build inputs**

```bash
git rm docker/vps/.doco-cd/proxy.py docker/vps/.doco-cd/Dockerfile
```

- [ ] **Step 5: Validate locally**

```bash
cd /home/tscibilia/home-ops
docker compose -f docker/vps/.doco-cd/docker-compose.app.yaml config >/dev/null && echo "compose OK"
bash -n docker/vps/.doco-cd/cron-update.sh && echo "cron-update syntax OK"
just docs test
```

Expected: all three pass. `just docs test` must stay green — `.doco-cd/` is not a numbered stack, so the generated tables should not change.

- [ ] **Step 6: Commit**

```bash
git add -A docker/vps ansible/vps
git commit -m "refactor(vps): pull akeyless-proxy image instead of building it

Replaces the locally built sidecar with ghcr.io/tscibilia/akeyless-proxy
pinned by digest. Keep-alive stays on here: this host resolves 21 secrets
per deploy."
```

- [ ] **Step 7: Deploy and verify** *(ask first — live host)*

```bash
ssh -i ~/.ssh/home-ops -p 22222 ubuntu@vps.internal \
  'sudo /opt/doco-cd/cron-update.sh && docker ps --filter name=akeyless-proxy --format "{{.Image}}\t{{.Status}}"'
```

Expected: image is the pinned ghcr reference, status `healthy`. Then confirm a deploy resolves secrets:

```bash
ssh -i ~/.ssh/home-ops -p 22222 ubuntu@vps.internal \
  'docker restart doco-cd && sleep 60 && docker logs --since 60s doco-cd | grep -c "failed to resolve external secrets"'
```

Expected: `0`.

---

### Task 11: Adopt the image on `truenas` and `clonenas`

**Files:**
- Modify: `docker/truenas/.doco-cd/docker-compose.app.yaml`, `docker/clonenas/.doco-cd/docker-compose.app.yaml`
- Modify: `docker/truenas/.doco-cd/cron-update.sh`, `docker/clonenas/.doco-cd/cron-update.sh`
- Modify: `ansible/truenas/playbook.yaml`, `ansible/clonenas/playbook.yaml`
- Delete: `proxy.py` and `Dockerfile` under both `.doco-cd/` directories

**Interfaces:**
- Consumes: the same digest, proven on `vps` in Task 10.
- Produces: both NAS hosts on the published image with keep-alive **off**.

- [ ] **Step 1: Swap the build for a pinned image on both hosts**

For each of `truenas` and `clonenas`, replace the `akeyless-proxy` service:

```yaml
  akeyless-proxy:
    image: ghcr.io/tscibilia/akeyless-proxy:1.0.0@sha256:<DIGEST>
    container_name: akeyless-proxy
    restart: unless-stopped
    networks:
      - doco-cd
    expose:
      - "8080"
    environment:
      AKEYLESS_ACCESS_ID_FILE: /run/secrets/akeyless_access_id
      AKEYLESS_ACCESS_KEY_FILE: /run/secrets/akeyless_access_key
    secrets:
      - akeyless_access_id
      - akeyless_access_key
```

`PROXY_KEEPALIVE` is deliberately absent. These hosts resolve one secret each; keep-alive's 30s idle timeout logs `Request timed out: TimeoutError`, which their fluent-bit `(?i)error` rule counts as an error. This is the regression that home-ops `5a32a13bd` fixed — do not add the variable here.

- [ ] **Step 2: Apply the same `cron-update.sh` edits to both**

Delete the `Dockerfile` and `proxy.py` fetch lines; change `--build` to `--pull always`, exactly as in Task 10 Step 2.

- [ ] **Step 3: Remove the Ansible copy tasks in both playbooks**

Delete `doco-cd | Copy Dockerfile` and `doco-cd | Copy proxy.py` from `ansible/truenas/playbook.yaml` and `ansible/clonenas/playbook.yaml`.

- [ ] **Step 4: Delete the local build inputs**

```bash
git rm docker/truenas/.doco-cd/proxy.py docker/truenas/.doco-cd/Dockerfile \
       docker/clonenas/.doco-cd/proxy.py docker/clonenas/.doco-cd/Dockerfile
```

- [ ] **Step 5: Confirm no copies remain**

```bash
cd /home/tscibilia/home-ops
find docker -name proxy.py -o -name Dockerfile | grep -c . || echo "0 - none left"
docker compose -f docker/truenas/.doco-cd/docker-compose.app.yaml config >/dev/null && echo "truenas compose OK"
docker compose -f docker/clonenas/.doco-cd/docker-compose.app.yaml config >/dev/null && echo "clonenas compose OK"
just docs test
```

Expected: no `proxy.py` or `Dockerfile` under `docker/`, both composes valid, docs green.

- [ ] **Step 6: Commit**

```bash
git add -A docker/truenas docker/clonenas ansible/truenas ansible/clonenas
git commit -m "refactor(nas): pull akeyless-proxy image instead of building it

Both NAS hosts move to the same pinned image as vps. PROXY_KEEPALIVE is
omitted on purpose: they resolve one secret each, and the idle timeout
trips their bare (?i)error log matcher. See 5a32a13bd."
```

- [ ] **Step 7: Deploy and verify** *(ask first — live hosts)*

Run `cron-update.sh` with sudo on each host, then confirm no timeout lines appear:

```bash
docker logs --since 10m akeyless-proxy | grep -c "Request timed out"
```

Expected: `0` on both.

---

### Task 12: Documentation and close-out

**Files:**
- Modify: `docs/context/docker-hosts.md`
- Close: home-ops issue #3367

**Interfaces:**
- Consumes: the completed Phase 2 changes.
- Produces: docs that match reality.

- [ ] **Step 1: Update the aKeyless section**

In `docs/context/docker-hosts.md`, replace the description of a locally built `proxy.py` sidecar with the pinned image, and state the keep-alive split:

```markdown
## aKeyless on Docker Hosts

All three hosts run `ghcr.io/tscibilia/akeyless-proxy`, pinned by digest in each
host's `.doco-cd/docker-compose.app.yaml`. The source lives in
[`tscibilia/akeyless-proxy`](https://github.com/tscibilia/akeyless-proxy); nothing
is built on the hosts.

`PROXY_KEEPALIVE: "1"` is set on **vps only** — it resolves ~21 secrets per
deploy. The NAS hosts resolve one each; enabling keep-alive there makes the
proxy log an idle timeout that their `(?i)error` fluent-bit rule counts as a
fault.
```

- [ ] **Step 2: Regenerate and verify docs**

```bash
cd /home/tscibilia/home-ops
just docs generate
just docs test
```

Expected: both pass. Prose edits are preserved; only generated blocks change.

- [ ] **Step 3: Commit**

```bash
git add docs/context/docker-hosts.md
git commit -m "docs(context): akeyless-proxy is a pinned image, not a local build"
```

- [ ] **Step 4: Close the tracking issue**

```bash
gh issue close 3367 --comment "Done. proxy.py now lives in tscibilia/akeyless-proxy and ships as a pinned ghcr image consumed by all three hosts. Keep-alive stays vps-only per ADR-0003. Step 4 of the plan — porting the level-marker fluent-bit regex to the NAS hosts — is deliberately not included and remains open work."
```

- [ ] **Step 5: Open the follow-up**

```bash
gh issue create \
  --title "NAS hosts: adopt the level-marker fluent-bit regex" \
  --label "ready-for-human" \
  --body "truenas and clonenas still match a bare \`(?i)error\` substring in their 03-exporters fluent-bit config. vps uses a level-marker regex in docker/vps/05-observability/config/fluent-bit.conf. Porting it would remove the false positives that force PROXY_KEEPALIVE off on the NAS hosts. Blocked on nothing; deliberately excluded from the akeyless-proxy extraction."
```

---

## Self-Review

**Spec coverage.** ADR-0001 (adapter exists) → Tasks 1-6. ADR-0002 (standalone repo, published image) → Tasks 7-9, and the deletions in 10-11. ADR-0003 (keep-alive opt-in) → Tasks 5, 10 Step 1, 11 Step 1. ADR-0004 (public package, digest pin) → Task 9 Steps 3-4, and the `image:` lines in 10-11. Issue #3367's four proposed steps → Tasks 1-6 (step 1), Tasks 2-3 (step 2), Tasks 7-11 (step 3), Task 12 Step 5 (step 4, explicitly deferred).

**Placeholders.** `<DIGEST>` appears in Tasks 10 and 11. It is not a placeholder in the prohibited sense — it is a value produced by Task 9 Step 3, and the plan says so at the top of Phase 2. Everything else is literal.

**Type consistency.** `AKeylessClient` is constructed with `(access_id, access_key, ...)` in Tasks 1, 2, 3 and `main` (Task 6). `get_secret(name) -> dict` is defined in Task 3 and consumed by the handler in Task 4 and the `StubClient` in Tasks 4-5. `make_handler(client, keepalive)` is defined in Task 4 and called in Tasks 5 and 6. `ProxyServer(addr, handler_cls)` is defined in Task 4 and used in Tasks 4, 5 and 6. `read_credential(env, name)` is defined and consumed in Task 6.

**Known gap, deliberate.** No test asserts that the secret cache is not held across a token refresh; the two TTLs are independent by design (5 min vs 50 min) and coupling them would be a behaviour change, not a fix.
