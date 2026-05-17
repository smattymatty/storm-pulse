# Changelog

All notable changes to Storm Pulse are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This changelog starts at 0.1.4. Earlier versions (0.1.0–0.1.3) are not retroactively documented; consult `git log` for their history.

## [Unreleased]

### Added

- **Caddy integration** (`stormpulse/caddy/`). Long-running command `cellar_custom_domain_caddy_sync` writes a per-region Caddyfile fragment via the admin HTTP API (single POST to `/load` with `Content-Type: text/caddyfile`) and atomically persists it to a drop-in path. Optional `[caddy]` config section: `admin_url`, `main_caddyfile`, `drop_in_path`. Reached via the admin API rather than `docker exec` — no docker-socket permissions, no docker_binary or container_name fields.
- **Boot-time import verification** (`verify_drop_in_imported`). At agent boot, the main Caddyfile is parsed for `import` directives (exact paths or globs) resolving to the drop-in path. If no match is found, the agent refuses to start with a clear `ConfigError`. Catches silent "fragments written but never served" misconfiguration at start, not weeks later when a customer activation hangs.
- **`stormpulse caddy init` subcommand** (`stormpulse/cli/caddy.py`, `stormpulse/caddy/init.py`). Detects an installed Caddy under common search paths (`/etc/caddy/Caddyfile`, `/opt/caddy/Caddyfile`, `/opt/garage/Caddyfile`), prompts for admin URL + drop-in path with sensible defaults, runs the same import-directive check the agent uses at boot, and appends the `[caddy]` section. `--main-caddyfile` overrides auto-detection; `--force` overwrites an existing section. Mirrors `stormpulse garage init` ergonomically — no more hand-editing TOML to enable Caddy on a new regional VPS.
- **`ParamDef.max_bytes`** for opaque-content command params. The Caddyfile fragment param uses this (cap: 150 KB, ~1000 active custom domains per region) since regex validation can't sanely cover multi-line content with braces. `pattern` is now optional; `validate_params` requires at least one of `pattern` or `max_bytes` to prevent unvalidated-param footguns.

### Changed

- **`parse_caddy_json`** now passes cert-lifecycle log lines through instead of dropping them. Lines with a `logger` field starting with `tls` and a `msg` field (but no `request`/`status`) are preserved with `logger`, `msg`, `identifier`, `error`, `names` fields kept intact so the Storm-side `_detect_caddy_cert_event` classifier can route them. The agent does not classify event types — that's Storm's job. Includes real-captured fixture from a Caddy 2.6.2 + internal-CA spike (see `tests/logging/test_parsers.py`).

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

[Unreleased]: https://git.stormdevelopments.ca/official-public/storm-pulse/compare/v0.1.4...HEAD
[0.1.4]: https://git.stormdevelopments.ca/official-public/storm-pulse/releases/tag/v0.1.4
