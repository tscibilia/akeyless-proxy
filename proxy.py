#!/usr/bin/env python3
"""
Minimal aKeyless auth proxy for doco-cd's webhook secret provider.

doco-cd issues one HTTP request per secret with no auth chaining. aKeyless
needs a token from POST /auth before POST /get-secret-value will answer.
This bridges the two. See docs/adr/0001.
"""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


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

        self._secrets = {}
        self._secret_lock = threading.Lock()

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
