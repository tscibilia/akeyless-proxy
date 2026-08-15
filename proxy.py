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
