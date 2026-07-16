---
adr:
  id: "CORE-001"
  title: "Fitness functions: a decoupled enforcement suite"
  status: "Accepted"
  date: "2026-05-22"
  tags: ["fitness-functions", "ci", "enforcement"]
---

# ADR: Fitness functions

**Status:** Accepted

## Context

storm-pulse has four invariants worth mechanizing: [CORE-000](000-internal-module-architecture.md)'s two import rules (layer topology, no cross-boundary private imports) and two from the [Security Architecture](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Security-Architecture) (exactly three runtime dependencies, no `shell=True` in subprocess calls). All four are the kind of rule that a reviewer who hasn't internalized it will let through on a locally-reasonable change. They want to be checked by a machine.

The obvious move is to drop them in the pytest suite. The problem with that move is signal conflation: "a function returned the wrong value" and "an architectural boundary was crossed" are different events, and a red CI run should say which one happened. A fitness failure deserves its own signal, its own command, and its own job.

**Options considered:**

1. **Fitness checks as pytest tests in `tests/`** - the django approach. Simplest, reuses the runner. But a fitness failure reads as a test failure, which is exactly the conflation this ADR exists to fix.
2. **Pytest tests behind a marker** - `@pytest.mark.fitness`, excluded by default, invoked separately. Decoupled by convention; survives only as long as marker discipline does. A bare `pytest` that forgets the marker silently reintroduces the conflation.
3. **A dedicated non-pytest runner** - chosen.

## Decision

The suite is extensible: a later ADR that mechanizes a new invariant adds a function, and `python -m fitness` runs them all. It currently holds eight - the four founding checks (two enforcing [CORE-000](000-internal-module-architecture.md), two enforcing [Security Architecture](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Security-Architecture) invariants) plus four contract-and-boundary checks added by the integration ADRs.

| # | Function | Enforces | Mechanism |
|---|----------|----------|-----------|
| 1 | Layer topology | CORE-000 Rule 1 | `import-linter` |
| 2 | No cross-boundary private imports | CORE-000 Rule 2 | `fitness/` runner |
| 3 | No shell execution | Security Architecture, Layer 4 | `fitness/` runner |
| 4 | Runtime dependency allowlist | Security Architecture, supply chain | `fitness/` runner |
| 5 | Integration contract | CORE-005 (required core, first-party commands) | `fitness/` runner |
| 6 | State-merge fence | CORE-005 (one merge call site) | `fitness/` runner |
| 7 | External-loader no-execution | CORE-007 (P1 loader imports no package code) | `fitness/` runner |
| 8 | Wizard SDK purity & topology | CORE-007 (`sdk/` pure Foundation, `wizard/` imports no Feature) | `fitness/` runner |

**Function 1 - Layer topology.** `import-linter` contracts in `.importlinter` express CORE-000's four-layer model as layered contracts: Foundation below Framework below Features below Entry, with Features forbidden from importing sibling Features. Same tool the sibling django repo uses; shared tooling across the two Storm codebases is deliberate.

**Function 2 - No cross-boundary private imports.** A custom check walks every module in `stormpulse/` and asserts no module imports a `_`-prefixed name defined in another module. Dunder names (`__version__`, `__all__`) are exempt: they are public module metadata by convention, and the codebase already has four legitimate `__version__` imports that would false-positive without the exemption. `import-linter` can't express this - it reasons about packages and modules, not the privacy of imported names - so it lives in the `fitness/` runner.

**Function 3 - No shell execution.** Asserts no `subprocess` call in `stormpulse/` passes `shell=True`. The 2026-05-22 scan found zero occurrences, so this function is a regression guard against future creep rather than a cleanup tool. Mechanizes the Security Architecture's Layer 4 commitment.

**Function 4 - Runtime dependency allowlist.** Two assertions. (a) `[project.dependencies]` in `pyproject.toml` is a subset of `{websockets, psutil, cryptography}`. (b) No module in `stormpulse/` imports a third-party top-level package outside that set plus the standard library. Part (a) catches an undeclared dependency in the manifest; part (b) catches the bypass where a package is installed into the environment and imported without ever being declared. The integration SDK and wizard engine add nothing to this line: they are standard-library plus the same three packages, and a private integration's wizard code is standard-library plus the versioned `stormpulse.sdk` only (CORE-007).

**Functions 5-8 - contract and boundary checks added by the integration ADRs.** Function 5 (CORE-005) asserts every registered Integration satisfies the required core and that command-contributing Integrations are first-party (inside `stormpulse/`). Function 6 (CORE-005) fences the state-merge primitive to its one legal call site. Function 7 (CORE-007) asserts the external-package loader never imports or executes package code. Function 8 (CORE-007) asserts the wizard SDK's boundaries: `sdk/` stays pure Foundation (it imports no other `stormpulse` module and no host-mutation primitive, so external plugin code can trust it) and the `wizard/` engine imports only Foundation, never a Feature - which is what makes its capability-provider dispatch (a lookup by token, invisible to Function 1) safe. Each cites the ADR it mechanizes, per the governance rule below.

One candidate check - asserting every command in the registry uses an absolute binary path - remains unmechanized: most coupled to registry internals, hardest to mechanize cleanly. It stays a code-review concern until it earns its place.

**Mechanization.**

- Function 1 runs as `lint-imports`.
- Functions 2 through 8 live in a `fitness/` package at the repo root: a sibling of `tests/`, deliberately not under it and not listed in `[tool.pytest.ini_options] testpaths`. Plain Python, not pytest. `python -m fitness` runs them all.
- The `fitness/` runner runs every check and reports every violation before exiting non-zero, never fail-fast. Stopping at the first violation would hide the rest; the cost of decoupling from pytest is hand-rolled reporting, and the reporting has to be honest.
- `make fitness` runs the whole suite: `lint-imports && python -m fitness`.

## Consequences

**Positive:**

- A red `fitness` job is unambiguous: an invariant was crossed, not a behaviour changed.
- The two CORE-000 rules and two Security Architecture commitments stop depending on whether the reviewer happened to know them.
- The suite is gating from commit one; no warn-mode, no period where CI is green over a broken rule.
- New fitness functions have a home. Adding a check as a later ADR mechanizes a new invariant is a known-shape operation - the suite has grown from the founding four to eight this way.

**Negative:**

- A non-pytest runner means hand-rolled failure reporting. No pytest assertion introspection; the harness has to print clear, located, per-check failures itself.
- The plain-list baseline has no mechanical floor. A careless hand can add an entry instead of fixing a violation; the mitigation is review alone. The original 23 entries are burned down and the baseline ships empty, but the weak point stands as a guard against the next time the codebase grows debt.
- `import-linter` is a new dev dependency. Dev-only, so it doesn't touch Function 4's runtime allowlist, but it is one more tool in `[project.optional-dependencies] dev`.
- Two test surfaces now exist (`pytest` and `make fitness`). A contributor has to know to run both. CI runs both regardless; the cost is local muscle memory.

## Governance

**Automated enforcement.** `make fitness` runs in CI as its own job (per [CORE-002](002-release-and-ci-cd-pipeline.md)) and gates releases. A violation outside the baseline turns the job red and stops a release.

**Manual review.** Any new fitness function has to cite the document it mechanizes; a check with no ADR or Security Architecture clause behind it is rejected. A new baseline entry is rejected: the baseline only shrinks, a new violation is fixed not parked. Merging the fitness suite into pytest, or moving the checks behind a pytest marker, requires a new ADR superseding this one.

**Related ADRs:**

- [CORE-000 Internal module architecture](000-internal-module-architecture.md) - the rules functions 1 and 2 mechanize.
- [CORE-002 Release and CI-CD pipeline](002-release-and-ci-cd-pipeline.md) - runs this suite as a CI job and a release gate.
