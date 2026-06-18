# Spec: CORE-005 integrations wire payload

**Status:** Draft (implements [CORE-005](../adrs/core/005-integration-contract.md) decisions 4 and 5)

Pins the exact bytes the agent puts on the wire for Integration state, so the
storm-pulse refactor and the control-plane reader code against one contract. The
design is sealed in CORE-005; this fixes the JSON.

## Today (the shape being replaced)

`register` and `metrics.push` carry a top-level `garage` key:

```json
{ "type": "register", "...": "...", "garage": { /* GarageState.to_dict() */ } }
```

`GarageState.to_dict()` is `asdict` over node_id, hostname, zone, capacity_gb,
data_avail_gb, version, healthy, object_count, buckets[], keys[], peers[], and
`disabled_reason` (str | null, set only on GARAGE-000 self-disable, all other
fields zero-valued when set). caddy carries nothing on the wire today.

Two leaks (CORE-005 Finding 4): the Feature name `garage` is baked into the
protocol, and caddy has no wire presence to render.

## Target shape

One top-level `integrations` key replaces `garage`. It maps integration id to an
Integration report. The Feature name leaves the protocol; it becomes a map key.

```json
{
  "type": "register",
  "...": "...",
  "integrations": {
    "garage": { "status": "live", "disabled_reason": null, "state": { /* see below */ } },
    "caddy":  { "status": "disabled_error", "disabled_reason": "drop-in import missing", "state": null }
  }
}
```

### Integration report envelope

Every entry has the same three keys, integration-agnostic:

| Key | Type | Rule |
|-----|------|------|
| `status` | `"live" \| "disabled_error" \| "disabled_choice"` | required |
| `disabled_reason` | `str \| null` | non-null iff `status == "disabled_error"` |
| `state` | `object \| null` | the integration's own typed blob iff `status == "live"`, else null |

### status, and which integrations appear

- **`live`**: enabled, preconditions passed. `state` carries the integration's blob.
- **`disabled_error`**: enabled in config but a precondition or config check failed
  (CORE-005 decision 5, the soft-disable). `disabled_reason` names the cause. The
  control plane renders this **alarming**, distinct from disabled_choice.
- **`disabled_choice`**: present in config but `enabled = false`. Reported so the
  dashboard can show "off on purpose" rather than nothing. `disabled_reason` null.
- An integration **absent from config entirely** does not appear in the map.

This is the wire form of the disabled-by-error vs disabled-by-choice distinction
CORE-005 decision 5 requires. The alarming/normal rendering is the control plane's
job; the agent's job is to report the honest status.

### The `state` blob is integration-owned and opaque to the protocol

The protocol layer never types `state`. Each Integration owns its dataclass and
`to_dict()`; the control plane keys by id and parses per-integration. Garage's
`state` blob is today's `GarageState.to_dict()` **minus** `disabled_reason`, which
moves up to the envelope (no duplication). caddy's `state` is `{}` or a minimal
status blob when live; it has no discovery surface today.

## Migration (CORE-005: tolerate both shapes)

Field-presence keyed, no version negotiation needed for parsing:

1. **Control plane ships the tolerant reader first.** It reads `integrations` when
   present; else falls back to the legacy top-level `garage` key. This lands before
   or with the agent bump so no new-shape agent ever meets an old-shape-only reader.
2. **Agent bumps second.** Once the tolerant reader is live, the agent sends only the
   new `integrations` shape and stops emitting top-level `garage`.
3. **Legacy `garage` read is removed** in a later cleanup once no agent emits it.

The protocol **version** bump and the public Protocol-Specification wiki update are a
public-contract call reserved for the operator (CORE-005 governance). Parsing does
not depend on the version field; it depends on field presence, so the tolerant reader
is safe regardless of which version number is chosen.

## Acceptance

- No top-level `garage` key in any new-shape payload; `integrations` map only.
- garage live payload byte-identical to today's `GarageState.to_dict()` except
  `disabled_reason` relocated to the envelope.
- caddy appears in the map with a real status (`live` / `disabled_error` /
  `disabled_choice`), its first wire presence.
- The control-plane reader accepts both shapes and prefers `integrations`.
