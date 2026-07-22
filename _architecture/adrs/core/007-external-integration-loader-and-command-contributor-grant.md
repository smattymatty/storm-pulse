---
adr:
  id: "CORE-007"
  title: "External integration loader and the command-contributor trust grant"
  status: "Accepted"
  date: "2026-07-15"
  amends:
    - "CORE-005 (decision 8, confinement clause)"
  tags: ["architecture", "integrations", "loader", "security", "trust", "signing", "registry"]
---

# ADR: External integration loader and the command-contributor trust grant

**Status:** Accepted

## Context

[CORE-005](005-integration-contract.md) left the Integration runtime source-agnostic
but loader-less: the one hinge to the built-in set is the static
`agent/integrations_manifest.py`, and decision 8 held command contribution
first-party-only until a future loader ADR "defines signing, confinement, and a
per-plugin dependency allowlist." This is that ADR. It authorizes loading an
operator-sealed, privately-published, **first-party** integration onto an agent, the
motivating case being an integration that manages an out-of-process data-plane
service (an ingress reservation guard) and must receive control-plane-delivered
signed policy. Scope is the **operator-sealed private tier only**; untrusted or
public-publisher in-process code stays forbidden.

## Decision

**1. Augment, don't replace, the manifest.** The release-controlled built-in manifest
is retained; the loader adds a sealed external set from the installed ledger into the
same registry. Built-ins win every collision (D6), so both coexist; deleting the
built-in manifest is a later migration with its own evidence. A private checkout is
never added to `sys.path`: install records an immutable, content-addressed copy and
loads only from it. The installed tree is `stdlib` + the versioned Storm SDK only
([CORE-001](001-fitness-functions.md) Fn4 holds).

**2. Three authority layers, not authorship.** Repository path proves nothing.
Authority is layered:

- **Provenance** — an approved signer's signature over the exact digest.
- **Execution** — a local `integration_load` grant to import and run the adapter.
  This is the real privilege boundary: loaded code holds full in-process authority.
- **Remote-dispatch** — a **two-party** rule. Exposing `specs` to control-plane
  dispatch needs both the local `command_contributor` grant and an explicit
  product-owned control-plane allow rule bound to integration id, command name, and
  command-spec digest. Advertising a command authorizes nothing; this adds attack
  surface, not host power.

An execution grant without `command_contributor` exposes only non-command
capabilities (state, detection, targeted re-read, log-enrichment); the code stays
fully privileged regardless.

**3. Local seal; control plane mirrors, never grants.** A local CLI action seals
`{agent_id, integration_id, publisher_key_fingerprint, package_digest,
manifest_digest, command_specs_digest, granted_capabilities, sealed_at,
seal_format_version}`. Any digest change invalidates the grant; authority never
carries across versions. The control plane may propose, display the capability diff,
mirror seal status, and alarm; it may not create or expand a grant, dispatch a grant,
or load an unsealed digest (reusing [CORE-004](004-signoff-verify-hatch-and-seal.md)'s
mirror-never-seal model). Mirrored-metadata schema, persistence, and retention are
**not** specified here: durable storage is gated behind the four-question privacy
boundary (open decision on control-plane state). Pre-seal inspection is declarative
(verify signature, parse manifest, no import).

Revocation **fences, it does not unload**, and is capability-specific: revoking
`command_contributor` stops new dispatch while observation callbacks continue;
revoking `integration_load` fences every new callback but evicts imported code only
on agent restart. Rollback is permitted only to a previously sealed, non-revoked
digest.

**4. Two sealed artifacts + a composition seal** (for an integration that manages an
out-of-process service):

- **In-process adapter** — thin, `stdlib`+SDK, owns health/state, the command
  handler, and a management-socket client. The contract prohibits it from bundling or
  downloading the service binary (a sealed-trust term, see Limits).
- **Out-of-process service** — a separately-signed, digest-approved executable run
  under OS supervision as a restricted-user service, never a child of the agent,
  reached over a versioned, permissioned local socket.

A composition seal binds `{integration_digest, service_binary_digest,
service_manifest_digest, management_protocol_version, integration_sdk_version,
command_specs_digest}`; the adapter reports the service healthy only when the running
executable and protocol match it. Mutable signed-data the service consumes at runtime
(e.g. policy snapshots) is verified per-use by signer, scope, generation, digest, and
validity window: not an installed artifact, and it never rewrites the seal.

**5. v1 wizard: a minimal typed transactional SDK.** The integration returns typed
`Question`/`Finding`/`InitPlan`; the host owns rendering, validation, preview, ordered
application, verification, receipts, and rollback. "Transactional" means ordered
validated steps each with a compensating undo, rolled back in reverse — not atomic
commit across filesystem, service manager, and proxy; a failed compensation surfaces
loudly. v1 ships only the kinds the first consumer needs (text/confirm/choice/
secret-ref/path/port; ok/warning/refusal; typed mutations for immutable-binary
install, namespaced config, service registration, restricted dir/user, edge drop-in,
reload/restart, verify, rollback). No arbitrary shell, generic file writer,
service-manager control, or trusted-code escape hatch. The general schema language,
conditional-field DSL, and answer-file framework wait for a second integration.

**6. Collision and import failure.** A repeated id or command name is fatal to the
external package (built-ins win, quarantined with a named error). Ordinary import,
parse, or precondition exceptions soft-disable that one integration. A hang,
`os._exit`, or interpreter crash is **not** contained in-process (see Limits).

## What this ADR does and does NOT guarantee

These mechanisms establish **authorization, provenance, and topology — not runtime
confinement.** Stated plainly so nothing is oversold:

- A sealed adapter is **fully trusted in-process code**, equal in privilege to
  built-in agent code once verified, installed, and sealed. Signature, digest, seal,
  dependency limits, audit, and revocation are authorization and provenance, **not a
  sandbox.**
- This **amends CORE-005 d8's confinement clause** for this tier: confinement is
  *deferred, not claimed.* Real confinement (untrusted or less-trusted publishers)
  needs an out-of-process host with a narrow RPC and OS isolation — future work, own
  ADR.
- `try/except` contains ordinary exceptions only; a hang, `os._exit`, or interpreter
  crash can block or kill the agent.
- "No binary bundling" (D4) and "typed mutations only" (D5) are **contract terms,
  reviewable where statically detectable, not runtime-enforced.**
- Accepted v1 risk: locally-sealed private Python runs with agent authority, bounded
  by publisher + digest + per-agent operator authorization, **not by host
  capability.** Acceptable only for operator-sealed first-party code.

## Invariant — fail-closed is topological

An integration managing a hard boundary fails **closed by topology, not supervision**:
the protected upstream has **no path that bypasses the managed service.** The edge's
only route to the backend is through the service, with no fallback; if the service is
down the edge has no upstream and writes fail closed. Supervision restarting the
service is availability, not the safety property. The concrete edge/backend/service
binding lives in the managing integration's own docs.

## Consequences

- CORE-005's runtime contract is unchanged; only a load path and a capability/seal
  layer are added.
- A privately-published first-party integration can be installed and sealed on an
  agent without entering the public tree.
- A public plugin ecosystem is **not** enabled here and cannot be until the
  out-of-process host tier lands.

## Alternatives considered

- **Trust by authorship / repo path** — unverifiable at load time; replaced by signer
  + digest + local grant.
- **Control-plane-granted command contribution** — puts the crown-jewel authorization
  on the network surface and breaks CORE-004. Rejected.
- **Stay commandless, defer commands** — only postpones designing the command-trust
  surface, which must be designed once. Rejected.
- **Trust-flow-only wizard (run the package's own installer)** — verifies who, not
  what; replaced by the typed transactional SDK.
- **Claim in-process confinement from OS supervision or the SDK** — a false safety
  property; neither confines in-process Python. Rejected.

## Related

- [CORE-005](005-integration-contract.md) — the contract this loads; d8 is the gate,
  its confinement clause amended here.
- [CORE-001](001-fitness-functions.md) — the dependency principle, unchanged for the
  adapter.
- [CORE-004](004-signoff-verify-hatch-and-seal.md) — the mirror-never-seal authority
  model reused here.

## Amendment (2026-07-22) — runtime loader realization

The P1 surface (sign, trust, install, inspect) shipped without the runtime half.
Building it, `buckets_gate` (the guard's in-process adapter, D4) as the first
consumer, resolved these decisions:

- **Authoring surface.** An external adapter authors against a **versioned SDK
  declaration surface** (`SdkIntegration`, `SdkCommandSpec`, ...), never the
  internal registry. The loader translates a declared adapter into the internal
  `registry.Integration` + `config.CommandSpec` at load. This keeps `sdk/`
  Foundation-pure (Fn8) and makes "stdlib + versioned SDK only" literally true
  for an adapter. The v1 surface mirrors `CommandSpec` **in full**, which
  **supersedes D5's command-surface minimalism** for the command tier (the
  wizard/mutation minimalism of D5 stands); a declared `subprocess` command must
  pin its exact argv.
- **`command_specs_digest`.** A single SDK function over the **full declarative
  surface** (name, group, mode, timeout, params, flags, subprocess argv), sorted
  and canonical. Run identically by the publisher (to fill the manifest) and the
  host (to verify at load), so it cannot drift. The **handler callable is
  excluded** — its code is pinned by `package_digest`, not the specs digest.
- **Loader home.** The executing loader is `integrations/external/loader.py`.
  **Fn7 changes from a directory-level to a per-file no-execution fence**:
  install/inspect/digest/trust/manifest stay provably no-execution; `loader.py`
  is the one sanctioned executor.
- **Seal and revocation.** A seal grants **all** the package's requested
  capabilities (no à-la-carte grant). **Revocation stays capability-specific per
  D3**: revoking `command_contributor` fences new command dispatch while the
  adapter keeps loading for state/health; revoking `integration_load` fences
  every callback, evicting on restart. Loading reads sealed grants, never
  receipts.
- **Fn5.** Amended: a command-contributing Integration is first-party **or** a
  sealed external package holding a `command_contributor` grant.
- **Load mechanism (D1).** Sealed packages import through a **scoped
  `MetaPathFinder`** that resolves only sealed integration-ids (each unique and
  non-stdlib/non-`stormpulse` by the reserved-id rule) to their content-addressed
  tree. It never touches `sys.path`, and multi-module packages + relative imports
  resolve normally. The loader re-hashes the installed tree against the sealed
  digest at load (cheap defense-in-depth over the read-only content-addressed
  dir).
- **Command gate (D2).** Enforced **at registration**, not at dispatch: the
  loader registers an adapter WITH its translated commands only when
  `command_contributor` is effective **and** the recomputed `command_specs_digest`
  matches the seal; otherwise it registers **command-less** (loads for
  state/health under `integration_load`, advertises no commands, logs loudly). A
  digest mismatch therefore fences commands without dropping the adapter. The
  dispatcher and wire manifest stay grant-unaware.
- **Collision (D6).** Read literally: a repeated id **or** any single command-name
  clash with a built-in quarantines the **whole** external package with a named
  error (built-ins always win). Ordinary import/parse/precondition exceptions
  soft-disable that one adapter; the agent and its siblings stay up.
