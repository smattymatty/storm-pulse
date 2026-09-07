---
adr:
  id: "CORE-009"
  title: "The deploy investigation: a core probe, bounded by node-local config"
  status: "Accepted"
  date: "2026-09-07"
  authors:
    - "Mathew Storm (operator, decisions)"
    - "Claude (draft)"
  tags: ["architecture", "investigate", "contract", "deploy", "audit"]
---

# ADR: The deploy investigation

**Tier: timeless.** The shape of what a node may be asked to look at, and what
it may say back. No live figures, no path inventory. Verify against
`stormpulse/cli/investigate/__init__.py`, `stormpulse/sdk/investigate.py`, and
`config/stormpulse.example.toml`.

**Status: ACCEPTED 2026-09-07 (operator seal), at `c47cd99`.** Decisions 1 to 8
are sealed and NOT BUILT: no probe code exists, no config section is read, and
no node answers `stormpulse investigate deploy`. Decision 9 settles nothing and
says so: how a case file is declared on the wire defers to its own grill, and
that fork gates the first emit, not the first line of probe code.

## Context

On 2026-09-07 Storm's audit firm hit a wall on PBC-1: settling whether a guard
is deployed on the alpha node. The staff agent hand-assembled an SSH battery out
of README prose, spent two rounds disclosing `pgrep` self-match artifacts, and
filed the firm's first-ever tool wish. The wish asked for an SSH probe.

An SSH probe is the wrong answer twice. It gives the auditor a credentialed road
onto the box it audits, making it a control-plane actor over its own subject.
And it builds a second instrument beside one that already exists:
`stormpulse investigate` is one-shot, non-interactive, and already splits every
check fetch/judge so verdicts are testable without a box.

**What the battery found is why this ADR exists.** A guard binary *is* on alpha,
at `/home/storm/buckets-guard/storm-buckets-guard`: 16,917,304 bytes, modified
2026-07-17 18:25:02 UTC, residue from the July spike. Not wired, not running,
no unit. Every install site in the repo names `/home/storm/guard/` -- the
systemd unit's `WorkingDirectory`, `ExecStart`, `EnvironmentFile` and
`ReadWritePaths`, `storm-buckets-guard/CLAUDE.md`, `scripts/supervision-lib.sh`,
the pulse-adapter README, the `state_report.rs` tests. That path has never
existed on alpha. So the 2026-08-26 measurement checked a location that could
not have been right, and only three of its four legs were load-bearing. A repo
grep could never have settled this. The node did.

**The legitimacy question, and it is the sentence this ADR turns on.**
BUCKETS-033 decision 1 refused a whitelisted command that reads `guard.env`,
because it was a new node-reading capability obtained to learn a fact the node
already volunteers. Deployment *absence* is the one fact a node structurally
cannot volunteer: a box with no guard has no guard to speak, and a box with
residue in an unexpected directory has nothing that reports the residue. That is
the distinction that makes this probe pass where the `guard.env` read failed,
and it is the only distinction that does. An investigation that can be answered
by an existing emitted field does not get written.

## Decisions

### 1. `deploy` is a core investigation, not an Integration's

`stormpulse investigate deploy`, alongside `flaps`, `box` and `logs-pipeline`.

[CORE-005](005-integration-contract.md) decision 2 makes an Integration declare
an id, a config section and an `enabled` predicate. Main Site and Forgejo have
none, and they are nodes the question gets asked about. The question is loudest
precisely where the thing is *not* installed, which is the node that could not
host the Integration that would have declared the check. An Integration-declared
`deploy` would be unavailable on exactly the boxes it is for.

Generic over the estate: Main Site, Forgejo, Forgejo Worker, Garage, Storm
Buckets, Rclone Runner, generic VPS. Nothing in it names the guard.

Rejected: **a `guard deploy` investigation under the Buckets integration.**
Narrower to write, and it answers the alpha question. It answers no other node's.

### 2. The node judges evidence; it does not judge expectation

Two uses of "judge" meet here and the ADR separates them by name, because
collapsing them is how this decision gets misread later.

- **Fetch/judge** is internal and unchanged: fetches touch the host, judges are
  pure functions over the fetched text, so verdict logic is testable without a
  box (module docstring, `cli/investigate/__init__.py`). The node absolutely
  judges its own evidence. `judge_reboots` is the reference shape.
- **Expectation** is not the node's. The node reports what it sees: this unit
  exists in the user manager, this port has no listener, this binary sits at
  this path with this mtime. Whether that is what the box was *supposed* to be
  is the control plane's comparison, and the disagreement is the finding.

Nothing about *what should be true* crosses the wire inbound. The node cannot be
told what a healthy answer looks like, so it cannot be talked into one.

### 3. Parameters resolve from node-local config, and only from there

A new section, and naming it is part of this decision because "locally resolved"
is hand-waving until something names the surface it resolves from:

```toml
[investigate.deploy.storm-buckets-guard]
units = ["storm-buckets-guard.service"]   # checked in BOTH managers, see 6
expected_root = "/home/storm/guard"        # where the unit installs it
search_roots = ["/home/storm", "/opt/storm"]
ports = [6188]
max_depth = 3
max_bytes = 65536
```

Keyed by subject, so one node can answer for more than one thing and a node with
no `[investigate.deploy.*]` table answers `INCONCLUSIVE` naming the section it
lacks -- never `CLEARED`.

**Refused: a wire-supplied `artifact_glob`.** The tool wish asked for one; the
firm's signature has it as a parameter. Two reasons it cannot be one. `ParamDef`
(Security Architecture, Layer 3) rejects any declaration carrying neither a
`pattern` nor a `max_bytes`, and a glob's blast radius is not a property of its
own text: `**/*guard*` and `**/*` differ by two characters and by the whole
filesystem. A regex over the parameter cannot bound what the parameter does. And
[CORE-005](005-integration-contract.md) decision 8 keeps command contribution
first-party because baked argv templates are the security crown jewel; a path
arriving from the wire makes the control plane the chooser of what the node
reads, which is the same capability by a quieter door.

### 4. The search is bounded three ways, and the bound has a real cost

Root allowlist from node-local config, depth cap, byte cap on output.

**Refused: the unbounded home-wide search**, which is what the wish asked for
and what would have surfaced the alpha residue on the first pass. `$HOME` on a
Storm node holds `.config/stormpulse/` (the mTLS material and the HMAC key
path), `.local/share/stormpulse/` (the agent database) and whatever else the
operator account carries. A traversal with no root and no depth reads the
neighbourhood of every secret on the box to answer a question about one binary.

**The cost, stated rather than hidden: a bounded probe finds residue only in
roots someone thought to name.** It would have caught alpha, because
`/home/storm` is the obvious root and the residue is one level under it. It
would not catch residue somewhere nobody guessed. When the allowlist comes back
empty, the verdict is `INCONCLUSIVE` and the remedy line prints the exact `find`
the operator would run by hand. That is the no-self-escalation posture doing its
job: the probe names what it could not see instead of widening itself to see it.

Adding a root is a config edit on the box, which is a node-local decision by
someone standing on the node. It is not a repo change, and it is not a
dashboard field.

### 5. A binary outside `expected_root` is a finding, not an error

Inside a `search_root`, outside `expected_root`, is `IMPLICATED` with the path,
size and mtime on the evidence line. The probe does not conclude what it means;
it reports that the box disagrees with itself.

Alpha is the worked example, and this decision is the reason open question 4
settled the way it did: the repo says `/home/storm/guard`, the disk says
`/home/storm/buckets-guard`, and the *disagreement* is the whole finding. A
probe that only checked the expected path would have reported a clean CLEARED
absence and been exactly as wrong as the 2026-08-26 battery.

### 6. Unit checks run against both managers

System and user, every time, never one.

[CORE-003](003-rootless-install-mode.md) makes rootless the production default,
so the units that matter are frequently `--user` units. A system-only check on a
rootless box reports "no unit" for a unit that is running, which is a false
CLEARED: absence of evidence turned into permission, the precise thing
BUCKETS-032 refuses. Checking one manager is not a cheaper version of this
check. It is a wrong one.

### 7. `pgrep` self-match suppression is the contract's problem, not the caller's

The process scan excludes the probe's own pid and its process group. The firm
burned two rounds on this by hand on 2026-09-07; a probe that hands its caller
an artifact to disclose has moved work rather than removed it.

### 8. Only structure crosses the wire

The investigation builds a `CaseFile`. The CLI renders it human-first, as it
does today. The control plane renders it through `briefs/`. Two doors, one
engine.

**Refused: prose crossing the wire.** Prose is unversioned and undiffable:
[CORE-008](008-declared-wire-shape-for-emitted-state.md) Function 9 can fail a
build on a renamed field, and can say nothing at all about a reworded sentence.
A control plane that receives sentences has no contract, only a habit. It also
forecloses the second door, because a renderer that receives finished prose
cannot render it any other way.

### 9. A case file on the wire is a CORE-008 question, and this ADR does not answer it

Today `CaseFile`, `SuspectReport` and `Verdict` are CLI-local: nothing pushes
them, so nothing declares them.
[CORE-008](008-declared-wire-shape-for-emitted-state.md) governs the shape this
agent emits -- per emitted dataclass, the field names and their nesting, with
the digest advertised on register and its scope explicitly limited to names and
nesting, not types and not meaning. The moment a case file crosses the wire,
these three types are emitted shape and CORE-008 has jurisdiction over them.

**That is the whole of what is decided here: the jurisdiction, not the answer.**
Whether the case-file types are declared wholesale into `wire-contract.json` and
the digest, or whether the wire carries a narrower projection of them and the
rich types stay CLI-local, is a real fork with real consequences for every
future field a check wants to report. It is not settled by a draft, and it is
not settled as a side effect of an ADR about a probe. **It is owed its own
grill, and the node comes first there as it does here.**

Named rather than answered because it is a consequence the brief did not carry,
and a consequence found in the draft is a question for the grill, never a
decision the draft may take on its own authority.

## Consequences

- storm-pulse gains a check whose whole purpose is to report absence. Every
  other investigation explains a system that is running.
- The estate gains one instrument instead of two. The firm never gets a
  credential, a socket, or a road onto a box; it reads what the operator's own
  tooling already produced.
- A node with no `[investigate.deploy.*]` table answers INCONCLUSIVE, so rolling
  this out is a config change per node, visible and refusable, not a silent
  fleet-wide capability gain.
- Whether `CaseFile` becomes published contract is left open by decision 9, and
  that fork gates the first emit rather than the first line of probe code.
- The bound in decision 4 is a permanent, accepted blind spot. It is written
  down so that a future INCONCLUSIVE is read as the design working, not as the
  probe failing.

**Architectural Characteristics:**

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

**Fitness Functions:**

- **Bounded search (code-enforced, and the one that must exist before any code
  merges):** a test asserting the walk never leaves `search_roots` and never
  descends past `max_depth`, given a fixture tree that contains a match outside
  both. A refusal whose only defence is the author remembering it is not
  defended.
- **No wire-supplied paths (code-enforced):** the `deploy` investigation
  declares no `ParamDef` carrying a path or a glob; a test asserts its param set
  against an empty allowlist.
- **Both managers (code-enforced):** a judge-level test with a user-manager-only
  fixture that fails if the verdict reads CLEARED.
- **Self-match suppression (code-enforced):** a judge-level test feeding `pgrep`
  output containing the probe's own pid.
- **Declared shape (existing, deferred):** CORE-008 Function 9 covers whatever
  the grill in decision 9 settles as the emitted shape. Nothing to add here
  until it does.
- **Review-only, named as such:** decision 5's reading of an unexpected root as a
  *finding* rather than an error is a judgement about verdict semantics. No test
  distinguishes an honest IMPLICATED from a lazy one.

## Privacy considerations

This stanza is written although the answer is "no personal information", against
`adrs/README.md`'s no-empty-stanza rule and by the operator's resolution
(brief amendment, 2026-09-07). The reason: the *exclusion* here is a decision
with an enforcing bound, not an absence. BUCKETS-043 is the precedent.

- **What data.** Unit names and their manager, absolute paths inside the
  configured roots, file sizes and mtimes, listener state per configured port,
  process names and pids, and the agent id of the node reporting. Paths may
  contain an infrastructure account name (`/home/storm/...`); that is a
  service account on Storm's own hardware, not a customer identity.
- **What purpose.** Settling whether a named thing is deployed on a named node,
  which is a claim the operator and the audit firm both need to be falsifiable.
- **What bound.** The byte cap in decision 3 is the mechanism, and it is
  structural rather than a knob's default: the probe reports a file's
  *existence, size and mtime* and never opens it, so file contents cannot enter a
  case file even if a configured root pointed at something sensitive. Retention
  is the events plane's 14-day prune, inherited unmodified (DEVELOPER-016), not
  a new window.
- **Who sees it.** The operator, via the CLI on the node and the dashboard. The
  audit firm, via the case files and the manifest it reads. No customer surface,
  no automated decision.

`/law25-pipeda-review` is not engaged: nothing here collects, stores or acts on
personal information. If a future check ever reads a path under a customer's
data directory, that review runs first.

## Governance

The `deploy` investigation is registered where the other core investigations
are, and this ADR owns its bounds. [CORE-005](005-integration-contract.md) is
unchanged: this adds no Integration and no whitelisted command.
[CORE-008](008-declared-wire-shape-for-emitted-state.md) owns the emitted shape
and is not amended here; decision 9 records that the case-file types come under
its rule when they are first emitted, and defers how they are declared to its
own grill.

The control-plane half is deferred to a separate ADR (website
`developer/020`), which gates on this one because the dashboard renders what the
agent emits.
