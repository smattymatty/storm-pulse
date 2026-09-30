---
adr:
  id: "CORE-000"
  title: "A kernel with Integrations plugged in: the four-layer import discipline"
  status: "Accepted"
  date: "2026-05-22"
  tags: ["architecture", "imports"]
---

# ADR: Internal module architecture

**Tier: timeless** for the four layers, the two import rules and the kernel line.
The member table is **dated 2026-09-29**: members arrive (`rclone/` did, and the
table missed it), so verify it against `.importlinter`, its enforced mirror.

**Status:** Accepted 2026-05-22. Amended 2026-09-29, maintainer-ruled: the
Features layer is named as its two kinds, Core Features and Integrations, and the
kernel is defined. No import rule changed.

## Context

`stormpulse/` runs on every host Storm operates, as the most privileged process
on the box (host root in system mode, the rootless-Docker user in the hardened
mode of [CORE-003](003-rootless-install-mode.md), see the
[Security Architecture](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Security-Architecture)).
Its import graph has to be one a maintainer can hold in their head.

Storm Pulse is a kernel with Integrations plugged in. The kernel does what every
install shares: connect to the control plane, prove identity, dispatch commands,
ship metrics and logs, run jobs. It names no Integration. Each one is imported
once in `agent/integrations_manifest.py`, and the kernel iterates whatever
registered itself through the [CORE-005](005-integration-contract.md) contract,
which [CORE-007](007-external-integration-loader-and-command-contributor-grant.md)
opens to a private package. The layers below are how that line is kept honest
in the import graph.

[CORE-001](001-fitness-functions.md) mechanizes the rules written here, and
[CORE-002](002-release-and-ci-cd-pipeline.md) ships the package they structure.

## Decision

The `stormpulse/` package is organized into four layers. Every module and
subpackage belongs to exactly one. Imports flow downward only, and Features may
not import sibling Features.

| Layer | Members | May import |
|-------|---------|------------|
| **Foundation** | `protocol.py`, `config/`, `events.py`, `sdk/` | nothing intra-package |
| **Framework** | `commands/`, `init/`, `auth.py`, `integrations/`, `wizard/` | Foundation |
| **Features: Core** | `logging/`, `signoff/`, `metrics.py`, `enroll.py`, `status.py`, `system_inventory.py` | Foundation, Framework; not sibling Features |
| **Features: Integrations** | `garage/`, `caddy/`, `rclone/` | Foundation, Framework; not sibling Features |
| **Entry** | `agent/`, `cli/`, `__main__.py` | any layer |

**The kernel is every row but the Integrations.** A Core Feature is wired by
name from `agent/` and every install carries it. An Integration is reached only
through the contract registry, and adding one adds a package, not kernel code.
The two rows share one import rule, so `.importlinter` holds them as one layer.

- **Foundation** is the wire format, the config and the integration-contract
  substrate. `protocol.py` carries the message envelope and payload contracts.
  `config/` carries the TOML-backed dataclasses and, in `config/param_schema.py`,
  the JSON shape a command param declares. `events.py` carries the wide-event
  buffer every layer emits into. `sdk/` carries the versioned integration-wizard
  contract a private Integration is written against (CORE-007). Foundation
  imports nothing intra-package, so `sdk/` stays pure enough for external code
  to depend on.
- **Framework** is shared infrastructure: `commands/` is the runtime command
  registry and job runner, `init/` the install-time setup framework, `auth.py`
  HMAC and nonce verification, `integrations/` the contract registry
  (CORE-005), and `wizard/` the transactional mutation engine that applies an
  Integration's `InitPlan` with preview, per-step verify, receipt and a
  journalled rollback (CORE-007).
- **Features** are capability surfaces. Size is not the criterion: `metrics.py`
  is one module and `garage/` is thirty-eight, and the same rule binds both.
  Placement comes from the capability test, not the import shape.
- **Entry** is composition. `agent/` wires the running kernel and loads the
  Integrations. `cli/` and `__main__.py` are the command-line surface. Nothing
  imports Entry.

**Two rules govern imports.**

**Rule 1: Layer topology.** A module may import only from its own layer or a
lower layer, and Features may not import sibling Features. Circular imports are
a symptom of this rule being broken, not a separate rule.

**Rule 2: No cross-boundary private imports.** A single-leading-underscore name
(`_foo`, `_Bar`) is private to its defining module. No other module may import
it. To be used outside its file, a name has to be public. Dunder names
(`__version__`, `__all__`) are exempt: those are public module metadata by
convention.

**Two placements worth recording explicitly:**

- **`init/` is Framework, not its own layer.** Install-time setup and runtime
  dispatch are different lifecycle phases with the same architectural role,
  shared infrastructure Features depend on. A layer for one subpackage would be
  ceremony.
- **`init/orchestrator.py` stays in Framework, and feature setup is inverted.**
  The orchestrator once imported up into `garage` and `logging`. Rather than
  reclassify it to Entry, each Feature registers its install step through a hook
  in `init/registry.py` and the orchestrator iterates the registered steps. One
  hook buys an orchestrator that never changes when a Feature adds a step.

This ADR governs imports between modules. How a Feature arranges its own modules
is left to code review. Rule 2 still applies inside a Feature, since it is a
per-module rule, but intra-feature topology is not legislated here.

## Consequences

- A Feature's blast radius ends at itself and `agent/`. Removing `garage/`
  touches `garage/` and one manifest line.
- Underscore privacy is structural across the package, not a per-author courtesy.
- A module's legal dependencies are knowable from its layer alone.
- A helper that serves two Features is hoisted into Framework the moment the
  second consumer appears, even when that feels early. The friction is the
  feature.
- Rule 2 is stricter than the control plane's own topology rule, which
  constrains packages but not name-level privacy. A reader moving between the
  two codebases holds one extra rule here. The strictness buys binary
  checkability.
- Coupling that is not a static `import` (a dynamic import by string, a registry
  lookup by name, a dotted path) is invisible to both rules. It stays a review
  concern, with one machine-checked exception: `wizard/` reaches a Feature-owned
  capability by token, never by import, and CORE-001 Function 8 fences that seam
  and `sdk/`'s purity.
- 23 violations existed at adoption against an estimated 3. The cleanup is done.
  CORE-001's baseline mechanism was sized for the smaller number, and that
  trade-off is flagged there for re-decision.

## Governance

**Automated enforcement**, per [CORE-001](001-fitness-functions.md): Rule 1 by
the `import-linter` contract in `.importlinter`, Rule 2 by the `fitness/` runner.
Both run in CI under `make fitness` and gate releases (CORE-002). CORE-005's
contract registers the Integrations in Framework, so neither Foundation nor
Entry couples to one by name.

**Manual review** covers what the checks miss: dynamic imports, registry lookups
by name, dotted-path references. A Feature-on-Feature import is rejected in
review, and the resolution is to hoist the shared code into Framework, never to
grant an exception. A new module or subpackage is classified into the table
above on the commit that adds it. The table is the current-state record and
`.importlinter` its enforced mirror, and they must not drift.

**Related ADRs:**

- [CORE-001 Fitness functions](001-fitness-functions.md) mechanizes both rules.
- [CORE-002 Release and CI-CD pipeline](002-release-and-ci-cd-pipeline.md) publishes the package this ADR structures.
- [CORE-005 Integration contract](005-integration-contract.md) is the seam an Integration registers through.
- [CORE-007 External integration loader](007-external-integration-loader-and-command-contributor-grant.md) opens that seam to a private package.
