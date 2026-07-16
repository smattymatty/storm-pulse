---
adr:
  id: "CORE-002"
  title: "Release and publishing: manual, from the maker's workstation"
  status: "Accepted"
  date: "2026-05-22"
  tags: ["release", "pypi"]
---

# ADR: Release and publishing

**Status:** Accepted

## Context

storm-pulse ships as `pip install storm-pulse-agent` from PyPI. The agent runs on every VPS Storm operates and, even rootless (CORE-003), holds enough local privilege to manage that host's Garage and read its admin token. That makes a compromised PyPI credential the worst class of supply-chain attack: anyone who can publish storm-pulse-agent reaches every host that installs it.

Project-scoping limits the blast radius of a leaked credential to one package, but here that one package is the one that matters. The strongest mitigation is to keep no standing PyPI credential on any server. Publishing happens by hand, from the maker's workstation.

## Decision

**Release is a four-step act on the maker's machine:**

1. Bump `[project].version` in `pyproject.toml` and add the matching entry to `CHANGELOG.md`.
2. Commit. Push.
3. Locally: `make check` for the codebase, then `make pre-release-check` to assert `[project].version` and the top `CHANGELOG.md` entry agree.
4. `uv build && uv publish`. Pre-publish, smoke-test the wheel in a fresh venv: `pip install dist/storm_pulse_agent-*.whl && stormpulse --version`. Catches install-time breakage before PyPI's immutability makes it permanent.

**Version is static, in `pyproject.toml`.** Single source of truth, one line to edit. `setuptools-scm` is not adopted: a build-time dep and a layer of indirection to save editing one line.

**The wheel carries a second versioned surface: the SDK contract.** `stormpulse.sdk.SDK_API` is the integer version of the typed integration-wizard contract a private integration is built against ([CORE-007](007-external-integration-loader-and-command-contributor-grant.md)). It is distinct from `[project].version`: the package version moves every release, while the SDK contract version moves only when the wizard data types (`Question`/`Finding`/`InitPlan`) change incompatibly. A plan or manifest built against a newer `SDK_API` than the installed agent is refused at apply time. What `stormpulse update` does when an *installed* integration's SDK compatibility breaks - refuse the update, disable the integration and proceed, or hold the prior agent - is not yet defined: no external integration loads until the runtime loader lands (CORE-007's later slice), so there is nothing to break against yet, and that behaviour is specified there rather than pre-guessed here.

**Token never on a server.** The PyPI API token is project-scoped to `storm-pulse-agent` and stored only on the maker's workstation (env var / keyring). Compromise scope: that one dev machine. Rotation: at the maker's discretion, no shared secret to coordinate.

**Lockfile is hand-rolled.** `uv lock` is run when the maker bumps a dependency or wants to refresh, never on a schedule. The supply chain doesn't move without a deliberate local action.

## Consequences

**Positive:**

- No standing PyPI credential on any server Storm operates.
- No release-specific machinery to maintain. No tag-triggered workflow, no release secret in any store but the maker's own.
- The version-consistency check is a Python script the maker owns and runs, not a YAML step firing at an unseen moment.
- Release is a conscious moment: green checks are permission to publish, not the publish itself.
- One toolchain (uv) for build, lock, and publish.

**Negative:**

- Not a one-action release. The push is step 2 of 4; the publish is step 4. For a low release cadence this is fine; for daily releases it would be friction.
- No machine-enforced gate between "code is shippable" and "uv publish ran." If the maker publishes broken code, broken code goes up. The human-in-the-loop is the design, but it is a real cost.
- No provenance link between any external record and the PyPI artifact. The audit trail is `CHANGELOG.md` plus `git log -- pyproject.toml`, joined by version number.

## Governance

**Automated enforcement.** None at release time. `make pre-release-check` is local, run by the maker, not gated.

**Manual review.** Any proposal to publish from a server, to store a PyPI token on a server, or to add a release-triggered workflow that gates publish requires a new ADR.

**Related ADRs:**

- [CORE-001 Fitness functions](001-fitness-functions.md) - the `make fitness` suite invoked by `make check`.
- [CORE-000 Internal module architecture](000-internal-module-architecture.md) - the package structure that ships.
