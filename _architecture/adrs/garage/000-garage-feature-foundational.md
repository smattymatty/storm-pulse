---
adr:
  id: "GARAGE-000"
  title: "Garage Feature: docker-exec'd, v2-pinned, self-disabling"
  status: "Accepted (substrate precondition removed 2026-05-29)"
  date: "2026-05-29"
  tags: ["garage", "feature", "docker", "rpc-secret"]
---

# ADR: Garage Feature foundational decisions

**Status:** Accepted. The original ZFS substrate precondition was removed when [CELLAR-003](../../../../website/_architecture/adrs/cellar/003-zfs-substrate-for-garage.md) was amended (alpha provider's LVM-ext4 topology makes ZFS-on-clean-disk unworkable; durability moved one layer up into `garage.toml` via `metadata_fsync = true` and `metadata_auto_snapshot_interval`). That responsibility is now the 002-garage playbook's sign-off concern, not the agent's startup gate.

`stormpulse/garage/` is the Feature module the agent uses to operate Garage on a Storm box. Per [CORE-000](../core/000-internal-module-architecture.md), Features layer.

## Map

| Concern | Decision |
|---|---|
| Where Garage runs | Docker container. Agent never installs a host-side garage binary. |
| How agent talks to it | `docker exec <container> /garage <args>`. No admin API HTTP. No socket RPC. |
| Substrate | Filesystem-agnostic at the agent layer. The 002-garage playbook commits to a substrate (currently ext4 with `metadata_fsync = true` + auto-snapshots per CELLAR-003 amendment); the agent does not enforce. |
| Garage version | v2.x only. Tested against v2.3.0. Enforced at agent start. |
| Runtime config | `[garage]` section in `stormpulse.toml`: `enabled, container_name, garage_binary, docker_binary, config_path, state_push_interval_seconds`. Schema in `stormpulse/config.py`. |
| Command surface | Curated named set in `commands.py`. No raw `garage` shell. No caller-supplied flags. |
| rpc_secret | Lives inside the Garage container. Agent has no path to it on host. CLI is sole reader. |
| Secrets on the wire | `GarageKeyRef` structurally holds `(key_id, key_name, permissions)` only. Admin secrets ride the wire exactly once: in `JobOutcome.extras` returned by `garage_provision_customer_bucket`, captured by the dashboard's `CustomerBucket` row. Never on subsequent state pushes. |
| Multi-step ops | JobManager-orchestrated with reverse-order rollback. `provision_customer_bucket` is 5 steps; failures report `step_completed`, `step_failed`, `rollback_status`, `manual_cleanup_required`. |
| State sync | Dedicated loop on `state_push_interval_seconds` (default 30s). `collect_garage_state()` runs sync via `asyncio.to_thread`, stored on `agent._garage_state`, bundled into `metrics.push` by `build_metrics_envelope`. Initial state in the `register` payload via `discover_garage()`. |
| Post-mutation refresh | Any successful long-running `garage`-group command triggers immediate refresh + push (`post_success_hook` in `agent/garage_actions.py`). `garage_refresh` is the internal command for explicit refresh. |
| Customer concept | Not in the agent. Operates on Garage primitives. "Customer" in `provision_customer_bucket` is the caller's word; the agent sees opaque names. |

## Preconditions at agent start

Run before garage command registration. Any failure self-disables the Feature, no `garage_*` commands register, and the reason publishes on `GarageState.disabled_reason`:

1. `docker exec <container> /garage --version` → starts with `v2.`. Else `garage_version_unsupported`.
2. `docker exec <container> /garage status` → exits 0. Else `rpc_secret_unauthenticated` (or `garage_unreachable` if the container isn't running, docker is missing, or the call times out).

Substrate (filesystem-level CoW or fs-level fsync semantics) is no longer an agent-start gate. The playbook owns that contract via `metadata_fsync = true` and `metadata_auto_snapshot_interval` in `garage.toml`, and the 002-garage sign-off step verifies it. The agent trusts the playbook to have done the work.

## Consequences

- Misconfigured host fails at boot with a named reason, not at first command.
- Garage CLI internal protocol changes within v2 stay invisible to the agent.
- Agent process cannot read `/etc/garage/rpc-secret`. It is inside the container and there is no agent-side read path.
- `GarageKeyRef` cannot carry a secret. Adding such a field requires an ADR amendment.
- Provisioning is the one path where secrets cross the wire, captured once in `JobOutcome.extras` and stored dashboard-side.
- Multi-step rollback is real and reports partial-cleanup state. Operators can finish manually when rollback can't.
- Garage v3 fails closed until this ADR is amended.

## Non-goals

- Admin API HTTP. Future ADR if Garage ever deprecates the CLI.
- Host-installed garage binary. Container is the boundary.
- Multi-version compatibility. Exactly v2.x.
- Snapshot orchestration. Future GARAGE-* ADR.
- Multi-tenancy mapping. Lives in website's cellar ADRs.

## Implementation status

| Item | State |
|---|---|
| Garage v2 version check | `garage/preconditions.py:check_garage_version` |
| `garage status` smoke-call precondition | `garage/preconditions.py:check_rpc_secret` |
| `GarageState.disabled_reason` field | `garage/state.py:GarageState` |
| ZFS substrate precondition | removed (CELLAR-003 amendment) |
| `docker exec` CLI invocation | `garage/runner.py`, `garage/state.py` |
| State sync loop with configured interval | `agent/garage_actions.py` |
| `garage_refresh` internal command | `agent/garage_actions.py:40` |
| Post-mutation refresh hook | `agent/garage_actions.py:94` |
| JobManager 5-step rollback for `provision_customer_bucket` | `garage/provision_bucket.py` |
| `GarageKeyRef` "never the secret" | `garage/state.py:31` |
| `GarageState` in `metrics.push` + `register` | `agent/garage_actions.py:133`, `garage/discover.py` |
| Curated command surface | `garage/commands.py` |
| `[garage]` config schema | `stormpulse/config.py:130` |

## Related

- [CORE-000](../core/000-internal-module-architecture.md): puts `garage/` in Features.
- [CORE-004](../core/004-signoff-verify-hatch-and-seal.md): verify-hatch dispatch the 002-garage playbook uses.
- [CELLAR-003](../../../../website/_architecture/adrs/cellar/003-zfs-substrate-for-garage.md): substrate commitment this ADR enforces agent-side.
- [DEVELOPER-010](../../../../website/_architecture/adrs/developer/010-verify-block-matcher-contract.md): matcher contract the 002-garage sign-off rows use.
