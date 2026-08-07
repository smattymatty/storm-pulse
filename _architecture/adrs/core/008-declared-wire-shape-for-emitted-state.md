---
adr:
  id: "CORE-008"
  title: "Declared wire shape for emitted integration state"
  status: "Accepted"
  date: "2026-08-06"
  authors:
    - "Mathew Storm (operator, seal)"
    - "Claude (draft)"
  tags: ["architecture", "protocol", "fitness-functions", "contract"]
---

# ADR: Declared wire shape for emitted integration state

**Status:** Accepted

## Context

An Integration's `state` blob is JSON on the wire, and its field names are the
interface. `GarageState`, `GarageBucket` and `GarageKeyRef` are frozen
dataclasses serialized by `asdict`, so every attribute name is published
verbatim to whatever control plane consumes the push.

The inbound half of that boundary is already defended.
`tests/garage/test_admin_bucket_mapping_contract.py` pins a full
`GetBucketInfoResponse` as a golden fixture, because `_bucket_from_admin_info`
reads `bytes`, `objects`, `quotas.maxSize` and `keys[].accessKeyId` by exact
name, and a Garage upgrade that renamed one would silently degrade our
defensive `.get()` calls to zero.

The outbound half has nothing. Renaming `GarageKeyRef.key_id` is a
source-level refactor: mypy is happy, every test in this repo passes, the
wire tier passes, and the change is breaking for every consumer. The asymmetry
is the defect. We defend the shape we read and publish the shape we happen to
have.

The consumer cannot close this from its side either. It can only assert against
what it believes we emit, and a belief that is never checked against us is a
belief that goes stale without a symptom.

## Decision

**1. The emitted shape is declared, not implied.** A generated
`wire-contract.json` at the repo root lists, per emitted dataclass, the field
names and their nesting. It is checked in, reviewed like source, and published
alongside the [Protocol Specification](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Protocol-Specification)
so a consumer can read the contract without reading our source.

**2. Fitness Function 9 keeps it honest.** A new check in the `fitness/`
runner regenerates the shape from the live dataclasses and compares it to the
checked-in file. They disagree, the suite fails. Renaming a field is still
allowed and always was; what changes is that the rename must also update the
declared artifact, in the same commit, where a reviewer sees it as a diff to a
contract rather than a diff to a struct. This is [CORE-001](001-fitness-functions.md)'s
extensibility clause used as written: a later ADR that mechanizes a new
invariant adds a function, and `python -m fitness` runs them all.

**3. The digest is canonical and reuses the one we already have.**
`sdk/declaration.py`'s `command_specs_digest` already solves this exact
problem: a `sha256:` digest over a declared shape, key-sorted JSON with
canonical separators, deterministic enough that "the release author and the
host produce identical bytes." The wire digest uses the same canonicalization.
One hashing convention in this codebase, not two.

**4. The agent advertises the digest on register, computed at runtime from the
dataclasses.** Not read from the file. A digest read from a file can be stale
with respect to the process that sends it; a digest derived from the live
classes cannot. The file is the published copy, Function 9 is what stops the
copy drifting, and the advertised value is always true of the agent that sent
it.

**5. Scope: names and nesting. Not types, not meaning.** The digest changes
when a field is added, removed or renamed. It does not change when a field's
units change, when a string starts carrying a different encoding, or when a
count starts meaning something else. Saying so plainly is part of the decision:
a consumer that treats an unchanged digest as proof of semantic stability has
misread it. Semantic drift is what the wire tier and the golden fixtures are
for.

## Consequences

A rename is now a two-file change and a visible contract diff. That is the
entire point, and it is a small tax on a rare operation.

The artifact is public, stable and machine-readable, so a consumer can pin
against it instead of against prose. The prose in the wiki stays the
explanation; the file is the contract.

No new dependency. `json` and `hashlib` are standard library, so
[CORE-001](001-fitness-functions.md) Function 4's three-package allowlist is
untouched.

The register payload gains one optional string. Agents that predate it send
nothing, and a consumer that has never seen the field must treat its absence as
"unknown", never as "compatible". The protocol has carried optional additive
fields before and this is the same shape.

This does not make any consumer correct. It makes a change to what we publish
visible to a consumer at connect time, and it makes an undeclared change fail
our own build. Both are things nobody could do before; neither is a guarantee
that the other end reads us properly.
