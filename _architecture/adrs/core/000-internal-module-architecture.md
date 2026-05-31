---
adr:
  id: "CORE-000"
  title: "Internal module architecture: four-layer import discipline"
  status: "Accepted"
  date: "2026-05-22"
  tags: ["architecture", "imports"]
---

# ADR: Internal module architecture

**Status:** Accepted

## Context

This ADR governs the `stormpulse/` Python package: roughly 50 modules running on every VPS Storm operates. A compromised agent is root-equivalent on its host (see the [Security Architecture](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Security-Architecture)), so the import graph has to be one a single maintainer can reason about. It had grown organically: feature subpackages (`garage/`, `caddy/`, `logging/`), a command and job framework (`commands/`), an install-time framework (`init/`), wire-format and config modules (`protocol.py`, `config.py`), security primitives (`auth.py`), and process entry points (`agent.py`, `cli/`).

Two adjacent ADRs hinge on this one. [CORE-001](001-fitness-functions.md) commits to mechanically enforced fitness functions, and a fitness function can only enforce a rule that exists on paper. [CORE-002](002-release-and-ci-cd-pipeline.md) makes storm-pulse a versioned PyPI artifact, at which point the internal structure becomes something shipped, not just used.

## Decision

The `stormpulse/` package is organized into four layers. Every module and subpackage belongs to exactly one. Imports flow downward only, and Features may not import sibling Features.

| Layer | Members | May import |
|-------|---------|------------|
| **Foundation** | `protocol.py`, `config.py` | nothing intra-package |
| **Framework** | `commands/`, `init/`, `auth.py` | Foundation |
| **Features** | `garage/`, `caddy/`, `logging/`, `metrics.py`, `enroll.py`, `status.py` | Foundation, Framework; not sibling Features |
| **Entry** | `agent.py`, `cli/`, `__main__.py` | any layer |

- **Foundation** is the wire-format and config substrate. `protocol.py` carries the message envelope and payload contracts; `config.py` carries the TOML-backed dataclasses. Foundation imports nothing intra-package.
- **Framework** is shared infrastructure: `commands/` is the runtime command registry and job runner, `init/` is the install-time setup framework, `auth.py` is HMAC and nonce verification.
- **Features** are capability surfaces. Size is not the criterion: `metrics.py` is a one-module Feature, `garage/` is a fifteen-module Feature, and the same rule binds both. Placement comes from the capability test, not the import shape.
- **Entry** is composition. `agent.py` wires the running agent; `cli/` and `__main__.py` are the command-line surface. Nothing imports Entry.

**Two rules govern imports.**

**Rule 1 - Layer topology.** A module may import only from its own layer or a lower layer, and Features may not import sibling Features. Circular imports are a symptom of this rule being broken, not a separate rule.

**Rule 2 - No cross-boundary private imports.** A single-leading-underscore name (`_foo`, `_Bar`) is private to its defining module. No other module may import it. To be used outside its file, a name has to be public. Dunder names (`__version__`, `__all__`) are exempt: those are public module metadata by convention.

**Two placements worth recording explicitly:**

- **`init/` is Framework, not its own layer.** `init/` is install-time setup; `commands/` is runtime command dispatch. Different lifecycle phases, same architectural role: shared infrastructure Features depend on. A separate "Setup" layer for one subpackage would be ceremony.
- **`init/orchestrator.py` stays in Framework; feature setup is inverted.** The scan caught the orchestrator importing up into `garage` and `logging` three times. Composition is normally Entry-layer work, so one option was to reclassify the orchestrator to Entry (a zero-code change). The chosen option was to keep it in Framework and invert the dependency: each feature registers its install step through a hook in `init/`, and the orchestrator iterates the registered steps without importing any feature. That spends one registration hook (`stormpulse/init/registry.py`, landed) and buys an orchestrator that never needs editing when a new feature contributes its init step.

This ADR governs imports *between* modules. The internal sublayering of any single Feature - how `garage/`'s own `parse.py`, `s3.py`, `state.py`, `provision_bucket.py` relate - is left to code review. Rule 2 still applies inside a Feature (it's a per-module rule), but intra-feature topology is not legislated here.

## Consequences

**Positive:**

- Feature removal stays local. The blast radius of a change to `garage/` ends at `garage/` and `agent.py`.
- Underscore-privacy becomes structural across the package, not a per-author courtesy.
- A module's legal dependencies are knowable from its layer alone, without reading its imports.
- [CORE-001](001-fitness-functions.md) has two written rules to mechanize.

**Negative:**

- A helper that genuinely serves two Features must be hoisted into Framework even when that feels premature (the `prompt_confirm` / `restart_or_hint` case). Forcing the question "what layer does this really belong to?" the moment a second consumer appears is the feature, not a bug, but the friction is real.
- Rule 2 is stricter than the sibling django repo's CORE-001, which constrains topology but not name-level privacy. A reader moving between the two repos holds one extra rule for storm-pulse. The strictness buys binary checkability; the extra rule is the cost.
- Coupling that isn't a static `import` - dynamic imports by string, registry lookups by name, dotted-path references - is invisible to both rules and to CORE-001's checks. It stays a code-review concern.
- 23 violations existed at adoption against an estimated 3. The cleanup is done, but [CORE-001](001-fitness-functions.md)'s baseline-mechanism choice (a plain list, no ratchet) was sized for the wrong number. That trade-off is flagged there for re-decision rather than quietly corrected.

## Governance

**Automated enforcement** (per [CORE-001](001-fitness-functions.md)):

- Rule 1 is enforced by `import-linter` contracts in `.importlinter`.
- Rule 2 is enforced by the custom `fitness/` runner.
- Both run in CI as part of `make fitness` and gate releases per [CORE-002](002-release-and-ci-cd-pipeline.md).

**Manual review** covers what static checks miss: dynamic imports by string, registry lookups by name, dotted-path references. A feature-on-feature import is rejected in review; the resolution is always to hoist the shared code into Framework, never to grant an exception. A new module or subpackage is classified into a layer in "Current state" below on the commit that adds it.

**Related ADRs:**

- [CORE-001 Fitness functions](001-fitness-functions.md) mechanizes both rules.
- [CORE-002 Release and CI-CD pipeline](002-release-and-ci-cd-pipeline.md) publishes the package this ADR structures.
