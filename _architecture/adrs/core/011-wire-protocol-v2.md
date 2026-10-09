---
adr:
  id: "CORE-011"
  title: "Wire protocol v2: negotiated, declared, signed end to end"
  status: "Accepted"
  date: "2026-10-08"
  authors:
    - "Mathew Storm (maintainer, decisions)"
    - "Claude (draft)"
  tags: ["protocol", "contract", "security", "transport", "events"]
---

# ADR: Wire protocol v2

**Tier: timeless.** The shape of the agent-to-control-plane protocol from
version 2 on. Verify against `stormpulse/protocol.py`, `stormpulse/auth.py`,
`stormpulse/agent/wire_contract.py` and `wire-contract.json`.

**Status: ACCEPTED 2026-10-08 (maintainer seal) at the commit that lands
this line; proposed at `c4da57b`.** Twelve decisions grilled and
maintainer-ruled 2026-10-08; nothing built. Supersedes the versioning rules of the v1 Protocol
Specification wiki page, which is archived as `Protocol-Specification-v1`
with a dated deprecation banner. The control-plane half (the dual-speak
window, its tripwire, the fallback branches deleted) is its own ADR in the
control plane's repo.

**Architectural Characteristics:**

- Security: every envelope key is signed, `agent_id` and the request id
  included; the canonical form is injective by construction.
- Auditability: a node declares what it runs and what it speaks at connect;
  a mismatch is a sentence on the dashboard, never a silent drop.
- Maintainability: one artifact, generated from live classes, pins both ends;
  every "None on agents that predate" branch is deleted.
- Testability: golden vectors and regenerate-and-diff in CI on both ends.

## Context

v1 was cut in 2026-05 for one agent describing one host. Everything since
has routed around it: integration state arrived as a field-presence hack
("no version negotiation", CORE-005 said it would be a bump), node-local
gate integrations deliver their events through a command result with no
ack back, and a node's component versions never reach the control plane
(CORE-008 covers field names only). `v` is checked on one end: the agent
refuses `v != 1`, the control plane requires the key and never reads it.
An unknown `type` on the agent is caught, logged locally and dropped
(`stormpulse/agent/dispatch.py:80-84`), so a new message sent to an old
agent vanishes and nobody learns it. Eight of ten `register` fields are
`| None` for agents that predate them, and each `None` is a fallback branch
on the control plane. The public spec describes a wire no agent emits.

## Decision

### 1. The control plane carries the overlap, for a bounded window

Agents are v2-only once updated. The control plane accepts v1 and v2 by the
envelope `v` until the last node registers v2, then deletes the v1 branch on
a dated tripwire it names in its own ADR. Never a flag day, never permanent
dual-speak (that is how the CORE-005 legacy read is still there four months
on).

### 2. The version is negotiated at register

Every envelope carries `v`. `register` carries `speaks: [2, ...]`;
`register.ok` names the version the session uses, the highest both sides
speak, never lower (no silent downgrade). An unknown `type` is answered with
an `error` envelope and the socket stays open, on both ends. Three rules:
the error carries the failing `type` and a reason code only, never the
payload; an error is never an ack; a later v3 ships as `speaks: [2, 3]`
with no cut.

### 3. `wire-contract.json` grows to every message

Same generator, from live dataclasses (CORE-008 decision 4), now covering
the envelope, every payload with field types, units and optionality,
command specs and per-command result shapes; one digest over all of it.
Units live in names (`_bytes`, `_seconds`); `''` and absent are distinct
and declared; enums are closed; no JSON smuggled as a string. The digest
rides `register` beside `speaks`, so a mismatch is seen at connect. CI on
both ends regenerates and diffs: the agent fails when the committed file
differs from the generator, the consumer fails when its vendored copy's
digest is not the version it claims to speak. The Markdown spec becomes
narrative plus pointer, never the pin.

### 4. The MAC signs the whole envelope as canonical JSON

HMAC-SHA256 over the `canonical_digest` form (key-sorted, canonical
separators, ASCII-escaped) of every envelope key except `hmac` itself, so
an additive field is signed for free and can never be an injection point.
`nonce` and `ts` are inside. No floats in anything signed: integers and
strings only. `wire-contract.json` carries golden vectors (fixed envelope,
test secret, fixed MAC) asserted by both repos. The `v1\n...&k=v` line form
(`stormpulse/auth.py:86-117`) and its injectivity refusal patch are deleted.

### 5. A state push says how complete and how fresh it is

Still a full snapshot, never a partial. It adds `generation` (monotone per
session), `complete: bool`, `last_full_read_at`, per-item `read_at`, and
`changed_ids` since the last ack. The consumer deletes its absence debounce
and recomputes only what changed. No deltas, no tombstones.

### 6. One typed events channel for every source

`events.batch` is the channel for every source, node-local gates included.
Each event carries `source`, `seq` and a shape declared per source in the
artifact; undeclared fields are refused at the agent, not stripped at the
consumer. Ack by `batch_id` as today, plus `events.batch.nack{reason}`. The
command-result tunnel for gate events and its arming step retire; prose
journal lines pinned as evidence become typed events.

### 7. `register` is the node's full self-declaration, all required

`speaks`, the contract digest, per-integration `command_specs_digest` and a
`commands_fenced` reason, `components[]` as a generic `{name, version,
digest}` list that each integration fills from its own report (the public
schema names the shape, never a product), `boot_id`, `agent_started_at`,
`signoff_sealed`, and `in_flight` request ids (ids only, never params).
Required means present, never non-empty: `components: []`,
`integrations: {}`. A digest mismatch refuses commands, never the
connection; the node stays visible as "contract mismatch: agent X, control
plane Y".

### 8. Node-local contracts stay off the wire

The hint and touched files (gate to agent), certificate renewal (CORE-010,
HTTPS) and the gate's state report (which fills `components[]`) are not
protocol messages. Nothing already decided off the wire moves onto it.

### 9. The command contract is typed end to end

Typed JSON params; a per-command `result` shape declared in the artifact,
so an extra can never overwrite a standard field; a structured
`error {code, http_status, upstream_code, message}`; `failure_reason` a
closed enum with `unknown_command` distinct from `forbidden`; a terminal
`command.cancelled`; sequences as `steps[]` with `step_index` echoed on each
result. Stderr-substring classification on the consumer is deleted.

### 10. Minimised at the source

The agent emits the S3 operation, never the request path; the legacy
`client_ip` name is gone; `bucket_id` is required on object lines. Every
new field carries the four-question privacy stanza in the ADR that adds it.

### 11. The v1 page is archived, not grown

The wiki's `Protocol-Specification` is renamed `Protocol-Specification-v1`
with a dated deprecation banner at the top (deprecated date, the date the
control plane stops speaking v1, pointer to v2). The new
`Protocol-Specification` describes v2 only, its shape sections generated
from `wire-contract.json`.

### 12. One node at a time, the same gate each time

Per node: `register` shows `speaks: [2]` and a matching digest, the node's
proof harness exits 0, zero `error` envelopes and zero mismatches over a
24 h soak, event and push counts inside the node's own baseline, a dated
frictionbook row with the clock. Never two nodes in one day. Rollback is
`stormpulse update --version <previous>`; the control plane still speaks v1
until decision 1's tripwire, so a rolled-back node is served.

## Considered and rejected

- **Bare `v: 2`, no negotiation.** Smallest diff, but v3 is another hard
  cut with another dual-speak window.
- **Hand-written JSON Schema per message.** Mechanical parity, but a second
  source of truth beside the dataclasses and a validator in the hot path;
  CORE-008 chose live classes to kill exactly that drift.
- **Keep the line-form MAC, add two lines.** Params would stay strings, so
  typed params could not be signed, and two canonical forms would remain.
- **Deltas with tombstones.** Smallest wire, but a lost delta is a silent
  gap and the reconcile heuristics return as resync logic.
- **Gate events keep the drain command.** No gate change this cut, but the
  unacked tunnel, the arming step and the unenforced retention bound survive.
- **Minimal register, grow additively.** Required-ness is the payoff of a
  hard cut; every optional field keeps its fallback branch.
- **Flag day, or staging harness only then fleet.** No overlap code, no
  per-node rollback.

## Privacy

What data: agent and request ids, component names, versions and digests,
boot ids, item ids and timestamps, S3 operation names. What purpose: a
control plane that knows what each node runs and speaks, and reconciles
state it can trust. What bound: no request path, no peer name, no secret
and no personal information rides any v2 message; undeclared event fields
are refused at the source; the gate retention bound holds because events
are acked, never parked. Who sees it: the node's operator and the control
plane. The IP on log lines is reviewed separately before the first build.

## Never again

- A version field read on one end only.
- A message dropped without the sender learning it.
- "Optional, None on agents that predate it" as a compatibility strategy.
- A canonical form that two different inputs can share.
- A node whose component versions the control plane must guess.
