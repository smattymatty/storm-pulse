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
are BUILT at `1c73324` and first ran on the staging node 2026-09-07, where a
node with nothing declared answered INCONCLUSIVE naming the section it lacked.
Decision 9 settles nothing and says so: how a case file is declared on the wire
defers to its own grill, and that fork gates the first emit, not the probe.

**Amended 2026-09-07** (decisions 10 and 11, decision 3 widened), landed at
`af83171`: the first live run had every node hand-edited to declare a fact it
already knew about itself. Nothing reversed; typing removed, bounds kept.

**Amended 2026-09-16** (decision 12, sealed on the commit that lands this line:
find it with `git log -S"### 12. A node may expose" -- <this file>`): a node may
expose this investigation over a road the node itself bounds. The Context's
first ssh objection is amended and one Consequence is struck; every decision
1 to 11 stands unchanged.

*Compacted 2026-09-16. Shipped implementation narrative was removed, not lost:
`git log -p` on this path carries the full text.*

## Context

On 2026-09-07 an audit of this estate could not settle a question that should
have been trivial: whether a particular service was deployed on a particular
node. Answering it by hand meant assembling an ad-hoc SSH battery from prose in
a README, then spending two rounds disclosing `pgrep` self-match artifacts. The
request that came out of it was for an SSH probe.

An SSH probe is the wrong answer twice. ~~It gives the auditor a credentialed
road onto the box it audits, making it a control-plane actor over its own
subject.~~ **First half amended 2026-09-16 by decision 12: a road may exist when
the node holds the bound.** The objection was to an unbounded road, and it is
answered by a forced command rather than by refusing the road. The second half
stands unchanged and is the one that still refuses the wish as written: it
builds a second instrument beside one that already exists:
`stormpulse investigate` is one-shot, non-interactive, and already splits every
check fetch/judge so verdicts are testable without a box.

**What the battery found is why this ADR exists.** A guard binary *is* on alpha,
one directory sideways from the path every install site in the repo names, left
as residue from the July spike: not wired, not running, no unit. The path the
repo agrees on has never existed on that box, so the 2026-08-26 measurement
checked a location that could not have been right. A repo grep could never have
settled this. The node did.

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
no subject from any source answers `INCONCLUSIVE` naming what it lacks -- never
`CLEARED`.

**Widened 2026-09-07 after the first live run: two sources, one rule.** The
original wording made this table the only source, which meant hand-editing every
node to declare a fact the node already knew about itself. An Integration
descriptor may now contribute a subject, and the table above overrides it field
by field, including `enabled = false` to switch one off.

The security property is untouched, and this is the test to apply to any third
source: **the subject resolves from something installed on the box, never from
the wire.** A descriptor ships in a package and sits on disk beside the config;
the control plane still cannot name a path, a unit or a glob. What changed is
who typed it, not where it lives.

Precedence, in one line: descriptor default, then the node's table, field by
field. The operator is the last word on his own box, so a package update can
widen nothing he has narrowed.

**Refused: a wire-supplied `artifact_glob`.** The original request asked for one; the
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
CLEARED: absence of evidence turned into permission, which is exactly what an
evidence rule exists to refuse. Checking one manager is not a cheaper version of
this check. It is a wrong one.

### 7. `pgrep` self-match suppression is the contract's problem, not the caller's

The process scan excludes the probe's own pid and its process group. Two rounds
were burned on this by hand on 2026-09-07; a probe that hands its caller an
artifact to disclose has moved work rather than removed it.

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

### 10. An Integration contributes its subject; the node still overrides it

Added 2026-09-07. A descriptor declares a deploy subject the way
[CORE-005](005-integration-contract.md) already lets it declare commands,
detectors and investigations. Enabling `[buckets_gate]` yields the guard subject
with nothing typed.

**This does not move `command_specs_digest`.** That digest covers the command
surface only -- name, group, argv, timeout, mode, flags, param validators, "every
field a control-plane allow rule binds to". A subject is data, not a command, so
no allow rule binds to it and no pin has to move. Checked before this was
written, because the opposite assumption would have made this a fleet-wide
deploy-order problem instead of a package change.

Decision 1 is unchanged and this is a different axis: `deploy` stays a CORE
investigation because it must answer on boxes carrying no Integration at all.
An Integration contributing a subject to it is not the same as owning it.

**Refused: contributed subjects being authoritative.** It reads simpler, and it
would let a package update widen a search root on every node carrying that
integration with no local brake. Simplicity yields to security here, per the
ranked five and the conflict the corpus names most often.

### 11. The wizard derives the subject from the unit file; it does not interview

Added 2026-09-07, for boxes with no Integration: Main Site, Forgejo, a bare VPS.
`stormpulse investigate deploy` stays one-shot and non-interactive, per
`CONTEXT.md`'s sealed Investigation term, which explicitly avoids "wizard".
Authoring config is the wizard engine's job (CORE-007 decision 5), and it already
owns preview, ordered apply, per-step verify, receipt and rollback.

The flow offers the box's operator-installed units rather than asking for a
name, and on a pick reads `WorkingDirectory`, `ExecStart` and `FragmentPath` to
propose `expected_root`, `search_roots` and the subject name. You confirm or
edit.

The reason this is the right read and not merely the convenient one: a unit file
*is* the declaration of where its thing lives. Deriving `expected_root` from it
is what makes decision 5's finding possible, because anything outside it is then
outside by the node's own account rather than by a path someone remembered.

**The flow is composed in `cli`, not in `wizard`.** `.importlinter` puts `init`
and `wizard` on the same Framework line, so the existing unit lister in `init`
and the pure derivation in `wizard` cannot import each other; `cli` may import
both. The obvious reading -- that the wizard package owns the flow -- writes a
second systemd parser to be maintained forever.

**Derivation refuses rather than guesses.** A unit that names no
`WorkingDirectory` and no absolute `ExecStart` yields no subject, and `/` is
never an `expected_root` whichever property produced it. An invented root gives
a probe that reports cleanly about a place nothing was installed, which on
screen is indistinguishable from health. Receipt: the `/` guard existed on only
one of the two branches until an isolated mutation test exposed the other
(2026-09-07); the test that should have caught it was passing for a different
reason.

### 12. A node may expose this investigation over a road it bounds itself

Added 2026-09-16. An operator may put a dedicated key in a node's
`authorized_keys` behind `command=`, with `no-pty`, `no-agent-forwarding`,
`no-port-forwarding` and `no-X11-forwarding`, pointing at a runner that accepts
only `investigate`. A caller holding that key can run investigations on that
node and nothing else.

**The bound lives on the node, which is the whole decision.** An allowlist in
the caller is a promise the caller makes about itself, and a bypassed or buggy
caller keeps whatever access its key has. A forced command is enforced by the
subject, survives a compromised caller, and is revoked by deleting one line
without touching anyone else's access.

**The runner accepts what the installed CLI has, not a list it carries.** It
asks `stormpulse` which investigations exist and refuses every name and every
flag outside that answer. A new investigation is therefore reachable the moment
the agent that has it is installed, with no runner change and no fleet redeploy.
The node stays the authority on its own surface, the same rule decision 3
already applies to subjects.

**Refused: a caller-supplied window, pattern or config path.** `--grep` reaches
into journal content and `--config` repoints the probe at another subject,
which are the wire-supplied-path refusals of decisions 3 and 4 arriving by
another door.

**Refused: a run-everything mode.** A caller names the investigations it wants.
Nothing offers "all", because a shotgun default is how a bounded road becomes an
unbounded one without anyone deciding to widen it.

## Consequences

- storm-pulse gains a check whose whole purpose is to report absence. Every
  other investigation explains a system that is running.
- The estate gains one instrument instead of two. ~~The firm never gets a
  credential, a socket, or a road onto a box; it reads what the operator's own
  tooling already produced.~~ **Struck 2026-09-16 by decision 12.** An auditor
  may now hold a key restricted to this investigation. Auditor independence is
  the characteristic that bent: an auditor able to act on its own subject is
  weaker than one that cannot, and what buys the trade back is that the subject
  holds the bound. What was bought is an audit that does not stall waiting for a
  human to run a command, which on a solo-operated estate is the difference
  between a question answered today and one answered next week.
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
  judges are mutation-testable with no box in the loop. That is the seam to
  point a test-writing pass at when this grows code.
- **Simplicity**: one instrument, one vocabulary, no second probe. The
  investigation is a fourth core check, not a subsystem.
- **Simplicity paid for without spending security, at decisions 10 and 11.**
  The bend goes the usual way once, at decision 10's refusal, and nowhere else:
  the subject still resolves from something installed on the box, never from the
  wire. Recorded because a reader should not have to infer that no security
  concession was made.

**Fitness Functions:**

- **The runner refuses what the CLI does not have (code-enforced, decision 12):**
  a test handing the runner a name the installed CLI does not list, and a second
  handing it `--grep` and `--config`, failing if either reaches a subprocess. A
  forced command whose only defence is the runner author remembering it is not
  defended.
- **The node's road is what it should be (operator-run, decision 12):** a
  pre-flight that reads a node's own restricted-key line and compares it to the
  expected runner and options, rather than assuming the arrangement survived the
  last time someone edited that file. Check it, do not assume it: the 2026-09-16
  re-seal gate read green for the previous move's command because nothing
  compared the control to what it was guarding.
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
- **The node table wins (code-enforced, added 2026-09-07):** a test giving one
  subject from a descriptor and a narrower `search_roots` in the node's table,
  failing if the merged config carries the descriptor's roots. Decision 10's
  refusal is only real if precedence is tested; an override rule defended by
  the author remembering it is the same as no rule.
- **`enabled = false` switches a contributed subject off (code-enforced, added
  2026-09-07):** the disable path is the operator's brake and is easy to leave
  half-wired, since nothing else exercises it.
- **Review-only, named as such:** decision 5's reading of an unexpected root as a
  *finding* rather than an error is a judgement about verdict semantics. No test
  distinguishes an honest IMPLICATED from a lazy one.

## Privacy considerations

This stanza is written although the answer is "no personal information", against
`adrs/README.md`'s no-empty-stanza rule and by the operator's resolution
(brief amendment, 2026-09-07). The reason: the *exclusion* here is a decision
with an enforcing bound, not an absence.

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
are, and this ADR owns its bounds.

~~[CORE-005](005-integration-contract.md) is unchanged: this adds no Integration
and no whitelisted command.~~ **Corrected 2026-09-07 by decision 10:** this
still adds no Integration and no whitelisted command, but CORE-005's descriptor
gains a deploy-subject declaration alongside the commands, detectors and
investigations it already carries. That is a contract-surface addition and
CORE-005 is amended to name it. No allow rule binds to it, so
`command_specs_digest` does not move.
[CORE-008](008-declared-wire-shape-for-emitted-state.md) owns the emitted shape
and is not amended here; decision 9 records that the case-file types come under
its rule when they are first emitted, and defers how they are declared to its
own grill.

The control-plane half is deferred to a separate ADR (website
`developer/020`), which gates on this one because the dashboard renders what the
agent emits.
