# Changelog

All notable changes to Storm Pulse are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This changelog starts at 0.1.4. Earlier versions (0.1.0–0.1.3) are not retroactively documented; consult `git log` for their history.

## [Unreleased]

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

### Security

- New runtime SigV4 path is in-process and skips the existing `shell=False` discipline because there is no shell call. Layers 1–3 (no listening port, mTLS, HMAC + timestamp + nonce) still apply unchanged. See [Security Architecture — Layer 4](storm-pulse.wiki/Security-Architecture.md#layer-4----execution) for the updated trust model.
- Customer secrets in `garage_bucket_clear` params are documented as a known scope-limited exposure: the secret lives in agent memory for the job's duration, and the existing Layer 5 sandbox bounds the impact of in-process compromise. See [Security Architecture — What this architecture does NOT protect against](storm-pulse.wiki/Security-Architecture.md#what-this-architecture-does-not-protect-against).
- Runtime dependencies unchanged: `websockets`, `psutil`, `cryptography`. SigV4 was deliberately written against stdlib + existing deps rather than pulling in a vendor S3 SDK.

[Unreleased]: https://git.stormdevelopments.ca/official-public/storm-pulse/compare/v0.1.4...HEAD
[0.1.4]: https://git.stormdevelopments.ca/official-public/storm-pulse/releases/tag/v0.1.4
