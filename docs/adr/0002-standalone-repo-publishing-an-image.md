# Standalone repository publishing a container image

**Status:** accepted
**Date:** 2026-08-15

The adapter previously lived inside the repository that consumed it, as one hand-copied source file per host, each built locally by `docker compose --build`. This repository is now the single source, and it publishes one image that all consumers pull.

## Considered options

**Keep a copy per host in the consuming repository.** The status quo, and the reason this ADR exists. Copies diverge silently. In the case that prompted the extraction, one host's copy had accumulated a token cache and a secret cache while two others had neither and re-authenticated on every secret fetch; the improvement had been committed against a single host's directory and never propagated. Nothing detected the gap, because nothing was comparing the copies.

**One shared directory inside the consuming repository.** Removes the duplication without adding a repository. Rejected: a consuming repository's tooling tends to assume its directory layout means something — that every directory under a given root is a deployable unit, for example — and a shared source directory violates that assumption. Working around a consumer's conventions to host a container's source is the wrong trade, and it only helps consumers that live in that one repository.

**A separate repository publishing an image.** Chosen. The source has one home, CI builds and tests it once, and consumers reference an immutable artefact rather than copying files.

## Consequences

- Changing the adapter becomes a two-repository operation: release here, then bump the pinned reference in each consumer. Slower for a one-line fix, but drift is eliminated by construction rather than by discipline.
- Consumers no longer build anything locally. This removes a class of failure where a configuration-management tool rebuilds the image on every run, orphaning the image the running container references and leaving the deployment neither idempotent nor inspectable.
- Any consumer-side self-update mechanism stops needing the build inputs. It fetches its compose file and pulls the pinned image.
- Consumers can be rolled forward independently rather than all at once. See ADR-0004.
- This repository must stay small — one source file, its tests, and a build workflow. Growing a framework or a dependency tree is a signal the design went wrong, not that the project matured.
