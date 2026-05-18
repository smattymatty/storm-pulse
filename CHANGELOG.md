# Changelog

All notable changes to Storm Pulse are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This changelog starts at 0.1.4. Earlier versions (0.1.0–0.1.3) are not retroactively documented; consult `git log` for their history.

## [Unreleased]

## [0.1.6] - 2026-05-18

Adds Storm Pulse's first Caddy integration, enabling per-region custom-domain hosting on regional VPS hosts.

### Added

- **Caddy integration.** New `[caddy]` config section enables Storm Cellar's per-region custom-domain hosting. Run `stormpulse caddy init` to set up. See [Caddy Integration](storm-pulse.wiki/Caddy-Integration.md).
- **Boot-time Caddyfile import check.** The agent refuses to start if the main Caddyfile does not import the configured drop-in path — catches "fragment written but never served" misconfigurations at boot, not weeks later when a customer activation hangs.
- **`caddy_json` parser ships TLS cert events.** Log lines from `tls.*` loggers now pass through with cert-lifecycle fields preserved (`logger`, `msg`, `identifier`, `names`, `error`). Previously: silently dropped.

### Changed

- `ParamDef` now supports `max_bytes` for opaque-content params; declarations must set at least one of `pattern` or `max_bytes`. Affects custom-command authors only if they ship multi-line content params. See [Customize Commands — Parameters](storm-pulse.wiki/Customize--Commands.md#parameters).

## [0.1.5] - 2026-05-18

Adds customer bucket provisioning, alias management, and additional data-plane operations to the Garage integration. Lowers the default manifest cadence to 30 seconds.

### Added

- **Customer bucket provisioning commands.** `garage_provision_customer_bucket`, `garage_delete_provisioned_bucket`, `garage_provision_additional_key`, `garage_rotate_customer_key` — long-running, dispatched by Storm Cellar. See [Garage Integration — Customer bucket provisioning](storm-pulse.wiki/Garage-Integration.md#customer-bucket-provisioning).
- **Data-plane commands.** `garage_bucket_set_cors` (configure CORS rules) and `garage_walk_bucket_stats` (count objects and bytes under a prefix). Long-running, use the SigV4 client introduced in 0.1.4.
- **Alias management commands.** `garage_bucket_alias_global_add`, `garage_bucket_alias_global_remove`, `garage_bucket_alias_local_add`, `garage_bucket_alias_local_remove`. See [Garage Integration — Aliases](storm-pulse.wiki/Garage-Integration.md#aliases).
- **Tiered permission commands.** `garage_bucket_allow_rw` and `garage_bucket_allow_ro` split the `garage_bucket_allow` flow by tier, avoiding a conditional `permissions` parameter.

### Changed

- **Manifest cadence default lowered to 30s** (was 300s). Out-of-band Garage changes now reconcile in ≤30s. Existing installs should edit `state_push_interval_seconds = 30` in `stormpulse.toml`, or re-run `stormpulse garage init`.
- Provisioned bucket key names now use hyphens instead of underscores. Affects newly-provisioned keys only.
- `garage_delete_provisioned_bucket` orchestration now handles local aliases (detaches them before deleting the bucket) and cleans up orphaned keys (deletes keys whose only access was to the deleted bucket).

## [0.1.4] - 2026-05-05

This release introduces a long-running command pattern in the Storm Pulse protocol and ships the first command that uses it: `garage_bucket_clear`. It also extends the agent with a small, auditable SigV4 S3 client (no boto3) for talking to a local Garage data-plane endpoint.

### Added

- **Protocol: long-running command pattern.** New `command.progress` message type and `long_running` boolean on command metadata. Long-running commands emit one or more progress events between the originating `command.request` and the terminal `command.result`. See [Protocol Specification — Long-running commands](storm-pulse.wiki/Protocol-Specification.md#long-running-commands).
- **Agent job manager** (`stormpulse/commands/jobs.py`). Generic asyncio task substrate that any long-running command handler can plug into. Handles spawning, progress emission, terminal-result construction, and cancellation on agent disconnect. One `JobManager` per active WebSocket connection; jobs do not survive reconnects.
- **`garage_bucket_clear` command.** Bulk-deletes every object in a bucket via the local Garage S3 endpoint. Marked `long_running=true` and `sensitive_output=true`. Required params: `bucket_name`, `s3_endpoint`, `region`, `access_key_id`, `secret_access_key`. The customer's secret rides in the signed envelope, lives in agent process memory only for the job's lifetime, and is never persisted, never logged, and never appears in result payloads.
- **Hand-rolled SigV4 S3 client** (`stormpulse/garage/s3.py`). Two operations only — `list_objects_v2` and `delete_objects` — plus a `head_bucket` pre-flight. Built on stdlib + `cryptography` (already a runtime dep). No boto3, no new dependencies. SigV4 implementation verified against the AWS-published `get-vanilla` test vector.
- **Structured failure reasons for `garage_bucket_clear`:** `auth_failed` (HeadBucket rejected creds), `partial_failure` (DeleteObjects reported per-object errors — overall job marked failed; bucket counts left untouched), `os_error` (HTTP-layer failure during list or delete).
- **Top-level extras on `command.result`.** Long-running commands report command-specific summary fields at the top of the payload alongside the standard result fields. `garage_bucket_clear` reports `deleted_count`, `failed_count`, `errors[]` (each `{Key, Code, Message}`, max 10), `duration_seconds`, and `error` on failure.

### Changed

- `make_command_result(...)` now accepts an optional `extras` keyword argument. Extras merge into the wire payload at the top level. Used by long-running commands to deliver per-operation summary fields without inventing a new message type.
- `CommandDef` gains a `long_running: bool = False` field. Existing built-in commands and config-defined custom commands continue to behave identically; the field is opt-in.
- `register` payload's per-command metadata now includes `long_running`. Older agents that don't set it: dashboards should treat the absent field as `false`.
- Versioning rule clarified: new message types added within v1 are *additive but not silently ignored* — current parsers reject unknown types with `ProtocolError`. Deploy dashboard updates before agent updates that emit new message types.

[Unreleased]: https://git.stormdevelopments.ca/official-public/storm-pulse/compare/v0.1.6...HEAD
[0.1.6]: https://git.stormdevelopments.ca/official-public/storm-pulse/compare/v0.1.5...v0.1.6
[0.1.5]: https://git.stormdevelopments.ca/official-public/storm-pulse/compare/v0.1.4...v0.1.5
[0.1.4]: https://git.stormdevelopments.ca/official-public/storm-pulse/releases/tag/v0.1.4
