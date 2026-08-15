# akeyless-proxy

A small HTTP adapter that lets [doco-cd](https://github.com/kimdre/doco-cd) resolve Docker Compose secrets from [aKeyless](https://www.akeyless.io/).

doco-cd's webhook secret provider makes exactly one HTTP request per secret. aKeyless needs a token from `POST /auth` before `POST /get-secret-value` will answer. One request cannot do a token exchange, so this sits between them.

```text
doco-cd  ──GET /secret?name=/path──▶  akeyless-proxy  ──POST /auth───────────▶  api.akeyless.io
                                                      ──POST /get-secret-value─▶
```

Python 3.14, standard library only, no dependencies.

## Usage

```yaml
services:
  akeyless-proxy:
    image: ghcr.io/tscibilia/akeyless-proxy:1.0.0@sha256:<digest>
    container_name: akeyless-proxy
    restart: unless-stopped
    expose: ["8080"]
    environment:
      AKEYLESS_ACCESS_ID_FILE: /run/secrets/akeyless_access_id
      AKEYLESS_ACCESS_KEY_FILE: /run/secrets/akeyless_access_key
    secrets:
      - akeyless_access_id
      - akeyless_access_key

  doco-cd:
    image: ghcr.io/kimdre/doco-cd:0.109.2
    depends_on:
      akeyless-proxy:
        condition: service_healthy
    environment:
      SECRET_PROVIDER: webhook
      SECRET_PROVIDER_WEBHOOK_STORES: |
        name: akeyless
        version: v1
        url: "http://akeyless-proxy:8080/secret?name={{ .remote_ref.key | urlencode }}"
        method: GET
        json_path: "values(@)[0]"
```

Then reference secrets in `.doco-cd.yaml`:

```yaml
external_secrets:
  MY_VAR:
    store_ref: akeyless
    remote_ref:
      key: /docker/myhost/my-secret
```

Pin by digest — see [ADR-0004](docs/adr/0004-public-package-consumers-pin-by-digest.md).

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `AKEYLESS_ACCESS_ID` | — | aKeyless API key access ID |
| `AKEYLESS_ACCESS_ID_FILE` | — | Path to a file holding it. Preferred; use with Docker secrets |
| `AKEYLESS_ACCESS_KEY` | — | aKeyless API key access key |
| `AKEYLESS_ACCESS_KEY_FILE` | — | Path to a file holding it. Preferred |
| `PROXY_KEEPALIVE` | `0` | `1` enables HTTP/1.1 keep-alive and a 30s idle timeout |

One of `AKEYLESS_ACCESS_ID` / `AKEYLESS_ACCESS_ID_FILE` is required, likewise for the key. The process exits non-zero at startup if neither is set.

### When to enable `PROXY_KEEPALIVE`

Turn it on only when a consumer resolves many secrets per deploy. Without it, doco-cd opens a new TCP connection per secret; a burst of 20+ can overflow the accept queue and fail the deploy with `connection reset by peer`.

Leave it off for low-volume consumers. The 30s idle timeout logs `Request timed out: TimeoutError` on every connection, which naive log-to-metric rules matching a bare `error` substring will count as a fault. See [ADR-0003](docs/adr/0003-keep-alive-is-opt-in-per-host.md).

## API

`GET /secret?name=<aKeyless path>`

| Status | Meaning |
| --- | --- |
| `200` | aKeyless response body, returned unmodified |
| `400` | `name` query parameter missing |
| `404` | Path other than `/secret` |
| `5xx` / `4xx` | Upstream aKeyless status and body, passed through |

The response is not reshaped, so the consumer owns the JMESPath. For a single static secret that is `values(@)[0]`.

## Caching

| Item | TTL | Why |
| --- | --- | --- |
| Auth token | 50 min | aKeyless tokens last 60 min; renew early |
| Secret value | 5 min | A deploy resolves the same secret repeatedly |

Both caches are process-local and in-memory. Restart the container to clear them.

## Development

```bash
python3 -m unittest discover -s tests -v
```

Tests stub the network — nothing in the suite reaches aKeyless.

## Documentation

- [ADR-0001](docs/adr/0001-http-adapter-over-doco-cd-configuration.md) — why an adapter exists at all
- [ADR-0002](docs/adr/0002-standalone-repo-publishing-an-image.md) — why this is its own repository
- [ADR-0003](docs/adr/0003-keep-alive-is-opt-in-per-host.md) — why keep-alive is opt-in
- [ADR-0004](docs/adr/0004-public-package-consumers-pin-by-digest.md) — packaging and versioning

## License

MIT
