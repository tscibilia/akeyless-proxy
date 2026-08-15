# Public package, consumers pin by digest

**Status:** accepted
**Date:** 2026-08-15

The image is published as a public package, and consumers reference it by immutable digest rather than by a moving tag:

```yaml
image: ghcr.io/tscibilia/akeyless-proxy:1.0.0@sha256:<digest>
```

## Considered options

**Private package.** Would require a registry login on every consumer host, a token stored per host, and a rotation story for those tokens. Rejected: the image contains no secret material. It is a single source file that reads credentials at runtime from files mounted by the consumer. Making it private buys obscurity and charges credential management on every deployment.

**Rolling tag.** Simpler configuration, but every consumer moves the next time it pulls — unattended and simultaneously. A bad release reaches all consumers at once, and rollback means rebuilding and republishing rather than reverting a line of configuration.

**Pinned digest per consumer.** Chosen. A consumer moves only when its own configuration is committed with a new digest, so a release can be proven on one deployment before the rest follow. Rollback is a one-line revert to a digest that is known to have worked.

## Consequences

- Publishing does not deploy. A release is inert until a consumer commits the new digest. This is the intended safety property, and it means "released" and "rolled out" are separate states that must be tracked separately.
- The digest must be surfaced where consumers will copy it from — the release notes and the build job's summary — because a digest that is hard to find encourages pinning by tag instead.
- Automated dependency tooling can bump the pin without special support: it is an ordinary image reference in a compose file.
- A public package means the image name and every layer are world-readable. No build input may reference internal hostnames, secret paths, or credentials. The image copies exactly one source file and installs nothing.
