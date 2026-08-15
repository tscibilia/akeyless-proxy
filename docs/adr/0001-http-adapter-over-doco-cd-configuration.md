# An HTTP adapter sidecar, not doco-cd configuration

**Status:** accepted
**Date:** 2026-08-15

doco-cd's webhook secret provider issues exactly one HTTP request per secret, with auth values supplied as static environment variables (`SECRET_PROVIDER_AUTH_TOKEN`, `..._APIKEY`). It has no request chaining and no pre-request step. aKeyless requires a token minted by a prior `POST /auth` call before `POST /get-secret-value` will answer. One request cannot perform a token exchange, so an adapter process is required.

This project is that adapter: a small HTTP server exposing `GET /secret?name=<path>`, performing the two-step aKeyless flow behind it, and returning the aKeyless response unmodified for doco-cd's JMESPath to parse.

## Considered options

**Configure the webhook store to call aKeyless directly.** The doco-cd wiki suggests this works. It does not, for two independent reasons, both verified against the live API on 2026-08-15 using deliberately invalid credentials:

- The documented URL `https://api.akeyless.io/v2/get-secret-value` returns `404 {"error":"unknown command 'v2/get-secret-value'"}`. There is no `/v2` prefix on the SaaS API — that path belongs to the self-hosted Gateway. Because this fails before authentication is ever attempted, it is easily misread as an auth problem; it was so misread in [kimdre/doco-cd#1224](https://github.com/kimdre/doco-cd/issues/1224).
- With the URL corrected, `POST https://api.akeyless.io/get-secret-value` returns `400 {"error":"invalid token"}`. It ignores `access-type`, `access-id` and `access-key` supplied in the body, and accepts only a `token`.

**Run the aKeyless Gateway as a sidecar.** The Gateway speaks the same API and requires the same token, so it does not remove the adapter — it only changes which host the adapter talks to. It also costs roughly 2Gi of memory plus a Redis dependency, against this adapter's single process.

**Mint a long-lived token and use it statically.** `POST /auth` accepts an `expires` parameter, so a long-TTL token could be placed in `SECRET_PROVIDER_AUTH_TOKEN` and the adapter dropped entirely. Rejected: it trades a self-renewing short-lived token for a long-lived bearer token sitting in an environment variable, and when it eventually expires every deployment on that host fails with no prior warning.

**Wait for a native aKeyless provider in doco-cd.** doco-cd ships providers for 1Password, 1Password Connect, OpenBao, Infisical, and the generic webhook. Nothing aKeyless-specific exists, and no upstream issue tracks it.

## Consequences

- This adapter is load-bearing infrastructure, not a temporary workaround. Any deployment resolving compose-time secrets from aKeyless through doco-cd depends on it being available.
- The adapter holds aKeyless API-key credentials. They are supplied as mounted files read via `*_FILE` environment variables and are never baked into the image.
- The adapter returns the upstream response body unmodified. Consumers own the JMESPath expression, so aKeyless response-shape changes surface in consumer configuration rather than requiring a release here.
- If doco-cd gains a native aKeyless provider, or a webhook `auth` store able to run a cached pre-request, this project becomes redundant and should be retired rather than maintained.
