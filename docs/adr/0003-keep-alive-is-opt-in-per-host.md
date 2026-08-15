# HTTP keep-alive is opt-in per deployment

**Status:** accepted
**Date:** 2026-08-15

The adapter defaults to `HTTP/1.0` with no persistent connections. A deployment that needs keep-alive enables it explicitly with `PROXY_KEEPALIVE=1`, which selects `HTTP/1.1` and applies a 30-second idle timeout.

This is deliberately not a uniform setting. Consolidating divergent copies into one implementation must not consolidate this particular difference, because both behaviours are correct — for different deployments.

## Considered options

**Keep-alive on everywhere.** Required by high-volume consumers. doco-cd resolves every external secret for a deployment in a burst, opening a fresh TCP connection per secret when connections are not reused. Python's default listen backlog of 5 cannot absorb that: the kernel resets the excess and the deployment fails with `connection reset by peer`. On the deployment where this was first diagnosed, roughly 20 secrets per deploy produced `ListenOverflows=5667` in about 10.5 hours of normal operation. Nothing crashes and nothing appears in the adapter's own logs, because the dropped connections never reach the handler — which makes it expensive to diagnose.

**Keep-alive off everywhere.** Required by low-volume consumers. A deployment resolving one or two secrets gains nothing from connection reuse, while the idle timeout logs `Request timed out: TimeoutError` on every connection. That line is benign, but log-to-metric pipelines commonly match a bare case-insensitive `error` substring rather than a structured level field, so each timeout is counted as an application fault and alerted on. Enabling keep-alive on such a deployment trades a real failure for a false one.

**A per-deployment toggle.** Chosen. Neither setting is correct for every consumer, and the correct setting is a property of secret volume, which only the consumer knows.

## Consequences

- The default is off, so a new consumer gets the quiet, low-throughput behaviour and opts in only when its secret volume justifies it. This is the safer default: the failure mode of leaving it off is a slow deploy, while the failure mode of leaving it on is spurious alerts.
- `Content-Length` is sent on every response regardless of protocol version. It is mandatory under HTTP/1.1 and harmless under HTTP/1.0, so the send path needs no branch.
- The enlarged listen backlog (`request_queue_size = 128`) and threaded serving are unconditional. They address the same burst problem, are harmless at low volume, and stop one slow upstream call from blocking unrelated requests.
- This toggle exists because of consumer-side log parsing, not because of anything in the protocol. A consumer whose log rules match a structured level field rather than a substring can enable keep-alive freely, and for such a consumer the distinction collapses.
