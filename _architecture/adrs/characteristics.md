# Architectural characteristics index

Every `**Architectural Characteristics:**` block in the ADRs under
`_architecture/adrs/`, extracted verbatim into one surface. Of the 10 ADRs
here, 1 names characteristics (4 bullets). Extracted 2026-09-09.

This index is extraction only: it holds what the ADRs already say, in the
words they say it. It does not judge. Re-derive any entry with:

    awk '/^\*\*Architectural Characteristics:\*\*/{on=1;next} on&&/^(\*\*|#)/{exit} on'
        _architecture/adrs/core/<adr>.md

Nine of the ten ADRs predate the house rule that an ADR carries a
characteristics block. That is recorded here as a count, not corrected here:
a characteristic is added by amending the ADR that owns it, and this index is
re-derived after.

## The system's five (operator-named, 2026-09-09)

**Security, extendability, auditability, maintainability, testability.**

Ranked by the operator 2026-09-09, in priority order: **1. Security,
2. Extendability, 3. Auditability, 4. Maintainability, 5. Testability.** This
order is the tie-breaker a trade-off consults: when two of the five pull
against each other, the lower number wins.

The website's five put simplicity in the second seat. The agent swaps it for
extendability, in the operator's words: "simplicity swapped out for
extendability." The reason is structural. The website is one product surface
Storm controls end to end. The agent is a substrate that runs on every host
Storm operates and must grow new capability without a redeploy of the whole:
a Feature per external system (CORE-000, CORE-005), a signed and sealed
external integration a private repository can contribute (CORE-007), a
declared wire shape a control plane can consume without reading the code
(CORE-008). Extendability is what those three ADRs exist to buy, and it is
bought under security, never over it.

**The named conflict: extendability against security.** Every seam that lets
the agent grow is also a seam an attacker would use, and the corpus
adjudicates the pair the same way each time: the seam exists, and it is
closed by default.

- The command registry is a whitelist of baked argv templates; the one
  operator-authored shell hatch ships sealed, unsealing takes typing the
  hostname back, and the unsealed state is nagged from every surface
  (CORE-004).
- Command contribution by an external package is allowed only for a
  publisher the operator approved, a package installed immutably by digest,
  and a digest the operator sealed on that host; the loader executes nothing
  before the seal (CORE-007).
- An Integration plugs into ten seams through one contract and a fitness
  function checks the contract, so a Feature cannot reach past it (CORE-005,
  CORE-001 function 5).
- The agent initiates every connection and no command reaches into a
  customer host from outside the whitelist (Security Architecture, cited by
  CORE-001 functions 3 and 4).

Recording the trade-off, not just the decision: extendability is second of
five, and each of those four is a place it yielded to first.

**The meta-characteristic: evolvability.** The five above are what the
architecture protects; evolvability is the wrapper that keeps them defended
while the system changes. In this repo it has a concrete mechanism rather
than an aspiration: `python -m fitness`, a dedicated non-pytest runner
(CORE-001), so an architectural boundary crossed reads as an architecture
failure and never as a test failure. A later ADR that mechanizes a new
invariant adds a function. The annex below is that runner's table.

## Summary

| ADR | Status | Date | Characteristics |
| --- | --- | --- | --- |
| [CORE-000](core/000-internal-module-architecture.md) Internal module architecture | Accepted | 2026-05-22 | 0 |
| [CORE-001](core/001-fitness-functions.md) Fitness functions | Accepted | 2026-05-22 | 0 |
| [CORE-002](core/002-release-and-ci-cd-pipeline.md) Release and publishing | Accepted | 2026-05-22 | 0 |
| [CORE-003](core/003-rootless-install-mode.md) Rootless install mode and host-native edge services | Accepted | 2026-05-25 | 0 |
| [CORE-004](core/004-signoff-verify-hatch-and-seal.md) Sign-off verify hatch and the ship-sealed default | Accepted | 2026-05-26 | 0 |
| [CORE-005](core/005-integration-contract.md) Integration contract | Accepted | 2026-06-18 | 0 |
| [CORE-006](core/006-signoff-identity-gate.md) Signoff identity gate | Draft (decisions sealed 2026-06-24 and 06-26; nothing implemented) | 2026-06-24 | 0 |
| [CORE-007](core/007-external-integration-loader-and-command-contributor-grant.md) External integration loader and the command-contributor trust grant | Accepted | 2026-07-15 | 0 |
| [CORE-008](core/008-declared-wire-shape-for-emitted-state.md) Declared wire shape for emitted integration state | Accepted | 2026-08-06 | 0 |
| [CORE-009](core/009-deploy-investigation-node-local-bounds.md) The deploy investigation | Accepted 2026-09-07 (operator seal, `c47cd99`) | 2026-09-07 | 4 |

## CORE-009 — The deploy investigation

Status: Accepted · 2026-09-07 · [ADR](core/009-deploy-investigation-node-local-bounds.md)

- **Security** over **auditability**, deliberately, at decision 4. A home-wide
  walk is strictly better at finding things and strictly worse at staying out of
  the credential neighbourhood. The five's priority order (security 1,
  auditability 3) settles it; the bent characteristic is auditability and the
  bend is the named blind spot, not a silent one.
- **Testability** is what the fetch/judge split buys and decision 2 protects: every
  verdict in this investigation is a pure function over fetched text, so the
  judges are mutation-testable with no box in the loop. That is the seam
  `/test-hunt` gets pointed at when this grows code.
- **Simplicity**: one instrument, one vocabulary, no second probe. The
  investigation is a fourth core check, not a subsystem.
- **Simplicity paid for without spending security, at decisions 10 and 11**
  (added 2026-09-07). The corpus's most-adjudicated pair is simplicity against
  security, and the first live run put it here: every node had to be
  hand-edited. The bend goes the usual way at decision 10's refusal (contributed
  subjects are defaults, not decrees, because authoritative ones would let a
  package widen a search root with no local brake) and nowhere else. Everything
  else in the amendment removes typing while leaving the bound where it was:
  the subject still resolves from something installed on the box, never from the
  wire. Simplicity gained without a security concession is not a trade-off, and
  it is worth saying so rather than recording a bend that did not happen.

Extraction note: CORE-009 names simplicity, which is not one of this repo's
five. It was written two days before the five were named for the agent, when
the website's five (simplicity second) were the only stated set. The words
stand as written; a future amendment to CORE-009 may restate them against
extendability, and this index is re-derived after.

## Enforcement status annex (derived, not extracted)

Unlike the website, where enforcement is classified per bullet after the fact,
this repo's enforcement is a single enumerated runner. CORE-001's table, as of
2026-09-09, is the whole annex:

| # | Function | Enforces | Mechanism |
|---|----------|----------|-----------|
| 1 | Layer topology | CORE-000 Rule 1 | `import-linter` |
| 2 | No cross-boundary private imports | CORE-000 Rule 2 | `fitness/` runner |
| 3 | No shell execution | Security Architecture, Layer 4 | `fitness/` runner |
| 4 | Runtime dependency allowlist | Security Architecture, supply chain | `fitness/` runner |
| 5 | Integration contract | CORE-005 (required core, first-party commands) | `fitness/` runner |
| 6 | State-merge fence | CORE-005 (one merge call site) | `fitness/` runner |
| 7 | External-loader no-execution | CORE-007 (P1 loader imports no package code) | `fitness/` runner |
| 8 | Wizard SDK purity and topology | CORE-007 (`sdk/` pure Foundation, `wizard/` imports no Feature) | `fitness/` runner |
| 9 | Declared wire shape | CORE-008 (`wire-contract.json` matches the emitting dataclasses) | `fitness/` runner |

Read against the five: functions 3 and 4 are security; 5, 6, 7, 8 are
extendability held under security (the seams exist, and each has a check that
they are used only through the contract); 1, 2 and 9 are maintainability. No
function names auditability or testability directly; CORE-004's sealed-by-
default hatch and CORE-006's identity gate are where auditability lives, and
both are enforced by the agent's runtime refusals rather than by this runner.
CORE-009 records its own testability mechanism (pure judges over fetched text).

Not mechanized, and said so: CORE-002's release discipline (a four-step act on
the maker's machine, guarded by `make pre-release-check` and by keeping no
publishing credential on any server) is procedure, not a fitness function.
CORE-003's two install modes are auto-detected at init and refused when forced
wrongly, which is a runtime check and not a tree check.

## Characteristic coverage map (derived, not extracted)

Dated 2026-09-09. One ADR carries characteristics, four bullets: CORE-009's
security-over-auditability bend is **R** (review-only, the ADR names it as a
blind spot by design); its testability claim is **F-partial** (the judges are
pure functions, and the mutation tests that would make it **F** are named as
the next seam, not yet written); its two simplicity bullets are **R**.

**Totals: 0 F / 1 F-partial / 3 R of 4.** The number to track is not that
ratio, which is one ADR's worth, but the count in the Summary column: 9 of 10
ADRs at 0. Each ADR that gains a characteristics block moves this index from
an annex about a runner to an index about a system. The next sweep re-derives
both against the tree and supersedes this note.
