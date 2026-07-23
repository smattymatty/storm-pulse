# Changelog

All notable changes to Storm Pulse are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.1] - 2026-07-22

The agent learns to run code it did not ship. This release cuts the CORE-007 runtime half: a signed private integration, installed and inspected without ever executing its code, can now be operator-sealed and loaded in-process, contributing commands under a two-party, digest-bound grant. The built-in manifest is untouched; the external set loads alongside it and built-ins win every collision. The guard's `buckets_gate` adapter is the first consumer.

### Added

- **External integration runtime loader (CORE-007 P3/P4)** (`stormpulse/integrations/external/loader.py`, `stormpulse/agent/external_adapters.py`). P1 installed and verified bytes; P2 signed and inspected them; this is where a sealed package finally runs. At agent bootstrap the loader reads the sealed grants, re-hashes each installed tree against its sealed digest, and imports the adapter through a scoped `MetaPathFinder` that resolves only sealed integration ids to their immutable content-addressed tree and never touches `sys.path`. The loaded `SdkIntegration` is translated into the internal registry contract and registered alongside the built-ins; a repeated id or command name quarantines the whole external package with a named error (built-ins win), and an import, parse, or precondition failure soft-disables that one adapter while the agent and its siblings stay up. `loader.py` is the one module in `external/` allowed to execute package code; install and inspect stay provably no-execution (Fn7 becomes a per-file allowlist).
- **The command gate is a two-party rule** (CORE-007 D2). An external adapter's commands are exposed to dispatch only when its sealed grant carries `command_contributor` **and** the command-spec digest recomputed at load still matches the seal; otherwise the adapter loads for state and health but advertises no commands. Fn5 is amended: a command contributor is first-party or a sealed external adapter holding that grant.
- **Grant seal, revoke, rollback** (`stormpulse/integrations/external/grants.py`; `stormpulse integration seal|revoke|rollback|grants`). A `SealedGrantV1` is the operator's execution authority, distinct from an install receipt: sealing re-hashes the installed tree and re-checks the publisher is still active, then grants the package's requested capabilities and points the node's active pointer at it. Revocation is capability-specific (CORE-007 D3): revoke `command_contributor` to fence new dispatch while the adapter keeps loading for state and health; revoke `integration_load` to fence everything (evicts on restart). Rollback re-activates a previously sealed, non-load-revoked digest. Each is an authority act gated by `--confirm-hostname`.
- **The SDK declaration surface** (`stormpulse/sdk/declaration.py`): `SdkIntegration`, `SdkCommandSpec`, `SdkParamDef`, `SdkJobOutcome`, `SdkProgress`, `SdkConfigError`, and the shared `command_specs_digest` an author and the host both run so a manifest's declared digest cannot drift from the code. This is the whole contract an external adapter is written against (stdlib + `stormpulse.sdk` only); the host translates it to the internal registry contract at load. The SDK now ships a `py.typed` marker, so an adapter type-checks the SDK as a typed dependency.

### Removed

- **`retention_days` is gone from `[[log_groups]]`.** The agent tails and ships; it stores no logs, so the knob enforced nothing (a dead knob under the CONTEXT.md "No dead knobs" rule). `stormpulse init` templates no longer emit it, and a stale key in an existing config logs a deprecation warning instead of failing, so deployed agents keep working. Log retention is a dashboard-side concern.

### Fixed

- **`stormpulse integration publisher` with no subcommand crashed** with an `AttributeError` instead of a usage message: the `publisher` subcommand group never carried the `--config` default the CLI entry reads. The publisher subcommand is now required (a clean argparse usage error), and the entry guards a missing config so no subcommand path can crash on it. Root cause was a test gap: the CLI tests drove `run()` with a hand-built namespace and never exercised the real argument parser, so a parametrized test now parses every `integration` subcommand through the actual parser and asserts each carries its config default.

## [0.3.0] - 2026-07-09

The agent learns to move data, not just manage it. This release cuts the rclone Integration: S3-to-S3 migration as agent jobs, with credentials that never touch disk and a restore test that proves the data comes back. It is the agent half of a control-plane-orchestrated import: the agent measures, transfers, and verifies; every decision about capacity and sequencing stays server-side.

### Added

- **rclone Integration: S3-to-S3 migration jobs** (`stormpulse/rclone/`). The third Integration and the first stateless one: config, preconditions, and three job commands, registered with one manifest import and zero kernel edits. `rclone_estimate` measures a source bucket (bytes + objects; any capacity decision happens control-plane-side), `rclone_migrate` pulls it (aggregate-only progress from rclone's JSON stats, per-object names dropped on the agent; re-dispatch to resume, skip-existing makes it idempotent), `rclone_restore_test` proves data comes back (a segmented sample - the largest object, the smallest, and one per folder - round-tripped through a scratch prefix in the same bucket, verified with `check --download`, scratch deleted on every path). Credentials arrive as secret-flagged params and reach the subprocess as env vars under `RCLONE_CONFIG=/dev/null`: never argv, never a file on disk, redacted from events and logs. The subprocess env is built minimal from scratch (nothing inherits from the agent's env), endpoints are https-only, and shutdown is SIGTERM-with-grace so rclone can abort in-flight multipart uploads before dying. `stormpulse rclone init` detects the binary and writes the `[rclone]` section, so configuring a Runner box does not mean hand-editing TOML (mirrors `garage init` / `caddy init`).
- **Migration progress carries transfer stats.** `rclone_migrate` progress frames report transfer rate, ETA, and object counts alongside bytes (rclone's log level raised to INFO so its JSON stats actually emit).
- **Job results carry their command's context.** The target `bucket_id` rides as a typed event field and the job's other small params (e.g. `max_size` on a quota set) ride the attrs long tail, so a quota event says which bucket it capped and at what value. Oversized params (a caddy tenants manifest) stay off the wire.

## [0.2.1] - 2026-07-04

The events plane: one wide, structured event per unit of agent work, shipped reliably to the control plane and analyzed at read time. Rate, p95, and every future summary are queries over raw events, never agent-side aggregates, so tomorrow's question is answerable from yesterday's data. Also in this release: the kernel goes garage-free (post-mutation refresh becomes a contract capability), and the first live catch of the events plane, a caddy sync race, is fixed.

### Added

- **Wide-event emission** (`stormpulse.events`). Foundation-tier `emit()` feeding a bounded in-process buffer; events ship as `events.batch` envelopes on the metrics cadence and are released only by dashboard ack, so a connection flap never loses the events that describe it. A full buffer drops oldest and the next drain prepends a `dropped_events` event: truncation is never silent. (Additive wire change: a new envelope type; older dashboards answer it with an error envelope and nothing breaks.)
- **Emit points.** Every Garage admin call (endpoint, method, duration, status, error text), every job result (durable `failure_reason` and stderr, the record that used to evaporate with the relay), and every reconnect (recorded while disconnected, shipped on the next session).
- **Events carry their target and their command.** Per-resource admin calls attribute their `?id=` to `bucket_id` or `key_id` by endpoint family, and a `command_ref` contextvar stamps every admin call a job's handler makes with the job's dispatch ref, so one command's whole story is queryable server-side.

### Fixed

- **Caddy sync drop-in persist race.** A sync is a full read-modify-write of a region's drop-in set; two running concurrently shared `site-<id>.caddy.tmp` and the loser's rename failed with `persist_failed`. Same-region syncs now serialize on a per-region lock inside the agent, since bursts of same-region dispatches are legitimate website behavior. Found by the events plane in its first minutes live, 2026-07-04.
- **`stormpulse update` docs said pip was the default source; the code's default is git.** The README now matches the code.

The kernel goes garage-free. Post-mutation refresh becomes a contract capability instead of garage-named orchestration inside the agent, so the last integration-specific module in the kernel is deleted and the third integration inherits the whole path for free.

### Changed

- **Post-mutation refresh is a contract capability.** An integration declares `read_affected(config, state, params)`, the targeted "which resources did this mutation touch, re-read only those" planner. The kernel owns everything after: the atomic snapshot merge and the metrics push. Garage moved to it as the reference implementer; `stormpulse/agent/garage_actions.py` is deleted. (Breaking for code importing that module.)
- **State types are named protocols.** A state blob implements `StateBlob` (`to_dict()`); an integration declaring `detect` or `read_affected` implements `MergeableState` (`with_items()`, the upsert merge, previously garage-only as `GarageState.with_buckets`). (Breaking for callers of the old method name.)
- **A command's `group` must equal its integration's id**, refused at startup with a soft-disable otherwise. The group is how the kernel maps a command back to its owning integration, so the coincidence is now a checked invariant.
- **Every metrics push carries the job-load snapshot.** Previously only the periodic push did; post-mutation, detector, and refresh pushes now ride the same single envelope builder, so a push sent right after a job completes reflects the queue that just changed. (Additive wire change: `jobs` appears on more pushes.)
- **Log enrichment is a contract capability, keyed by parser.** An integration declares `log_enrichers` ("my state can enrich lines of this parser"); the kernel wires each log group's parser to its declarer and rebuilds the enricher per batch from current state. Parser keys must be disjoint across integrations (fitness-checked). With this, the kernel carries zero integration names outside the registration manifest.

## [0.2.0] - 2026-06-21

Reworks the integration system into a single-source contract. This is the point of 0.2: authoring a command is now one entry that cannot be half-registered, an integration plugs into one seam instead of two, and "refresh my state" is a generic kernel capability rather than a per-integration special case. The advertised command manifest is byte-identical to 0.1.10, so no dashboard or website change is required to run this agent.

The footgun this closes: a command used to live in two parallel name-keyed maps that had to agree but were not 1:1, a schema (`CommandDef`) and, separately, a long-running handler factory. A command added to one but not the other surfaced at runtime as `Unknown command`, `Unknown params`, or a type error mid-dispatch. That class of bug recurred through earlier integration work. There is now one `CommandSpec` per command carrying both, so there is no second map to drift against.

New contributors should start at the [Architecture guide](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Architecture), which is the readable replacement for excavating internal decision records.

### Changed

- **One `CommandSpec` per command.** `CommandSpec` replaces `CommandDef` as the single registry type. It carries the command's schema and, for a long-running command, its handler. A new `mode` field (`subprocess` | `job` | `refresh`) is how the dispatcher routes; `long_running` is now a derived property of `mode`, not a separately-set flag. The advertised wire manifest is unchanged.
- **One integration command seam.** `Integration.commands` and `Integration.long_running` collapse into a single `Integration.specs` builder, so an integration contributes its whole command surface, schemas and handlers together, through one function. Garage and Caddy moved to it as the reference implementations.
- **`garage_refresh` is now generic.** "Collect this integration's state now and push it" is a kernel-owned capability synthesized as `{id}_refresh` for any integration that declares `collect_state`. Garage gets `garage_refresh` exactly as a third-party state integration would get its own, instead of a hardcoded special case in the dispatcher.
- **Command dispatch routes on `mode`** rather than a `long_running` bool plus a hardcoded command-name check.

### Removed

- **The parallel handler-factory maps.** The garage and caddy `long_running_factories` functions and `resolve_long_running_handler` are gone; a job's handler rides on its spec. The public builders `build_garage_commands` and `build_caddy_commands` are replaced by `build_garage_specs` and `build_caddy_specs`. (Breaking for code importing the old names.)
- **The internal `garage_refresh` special-case** in the dispatcher (the `== "garage_refresh"` magic string) and the bespoke `handle_garage_refresh` ceremony, replaced by the generic refresh routine.
- **`long_running` for config-defined `[[commands]]`.** A config command is always a subprocess (a job's handler can only come from an integration), so `long_running = true` in a `[commands.*]` table is now refused at load with an actionable error instead of silently producing a command that failed at dispatch. (Breaking only for configs that set it; it never functioned.)

### Added

- **Construction-time command guard.** A `CommandSpec` rejects illegal shapes the moment it is built: a `job` with no handler, a `subprocess` without an absolute binary path, or a non-`job` carrying a handler. Half-registration is structurally impossible now, not caught by a hand-maintained list of expected command names in a test.
- **Optional `StateBlob.summary()`.** A state object may expose a one-line summary used in its refresh result (garage reports `Refreshed: N buckets`); integrations without one get a generic line.
- **[Architecture wiki guide](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Architecture).** One page: the kernel/integration model, why it is shaped this way, the security and failure models, and a worked "write your own integration" walkthrough.

### Security

- **A broken command surface soft-disables its integration instead of crashing the agent.** A spec that fails to build at startup disables that one integration with a reported reason (matching the existing integration failure model), in-repo and third-party treated identically; the kernel and every other integration stay up. The Layer-4 whitelist invariants (absolute binary path, handler presence) are enforced at construction, not by a test's name-list, so a malformed privileged command cannot be built in the first place.

## [0.1.10] - 2026-05-31

Adds `run_apply_block`, the dashboard-driven apply-block sibling of `run_verify_block` introduced in 0.1.8. The website's command-block dispatch path sends `run_verify_block` for verify blocks and `run_apply_block` for apply blocks; the agent only implemented the verify side, so apply-block "Run (Pulse)" clicks were rejected as `Unknown command: 'run_apply_block'` and the operator had to fall back to **Mark run** plus a manual SSH paste. Discovered 2026-05-29 during the alpha-node 002-garage walk, where the Step 2 grype scan would not dispatch. The verify and apply hatches share the same seal: a sealed agent now auto-disables both. Also ships the rootless-install `--config` EUID-awareness fix queued from 0.1.9 cleanup work.

### Added

- **`run_apply_block` command.** Registered alongside `run_verify_block` in the standard command registry. Template `["/bin/bash", "-c", "{apply_command}"]`; the shell text arrives as a parameter, sized larger than the verify hatch (16 KiB cap, 600 s timeout) because apply scripts include image pulls, vulnerability scans, multi-line heredocs, and other multi-minute work that the verify limits (4 KiB, 30 s) were never sized for. Group `signoff`. Seal-gated identically to `run_verify_block`: when `stormpulse signoff seal` is in effect the agent removes `run_apply_block` from its registry and refuses any inbound dispatch with `failure_reason="signoff_sealed"`. ADR CORE-004's verify hatch and the apply hatch are one seal, two doors.
- **`stormpulse.agent.signoff_guard.APPLY_BLOCK_COMMAND` and `SEALED_COMMANDS`**. Public constants that name the seal-gated command set in one place so the dispatch-time recheck, the registry build-time check, and the sequence-pre-flight check cannot drift apart.
- **`stormpulse.init.files.default_config_path()`**. Public helper exposed via `stormpulse.init` that returns the EUID-appropriate `stormpulse.toml` path as a string. Use it as the `argparse` default for any new CLI subcommand that takes `--config`.

### Fixed

- **`--config` defaults are now EUID-aware across every subcommand.** Root → `/etc/stormpulse/stormpulse.toml` (legacy system install). Non-root → `$XDG_CONFIG_HOME/stormpulse/stormpulse.toml` (defaulting to `~/.config/stormpulse/stormpulse.toml`). Applies to `signoff` (status/seal/unseal), `caddy`, `garage`, `logging`, and `run`. `--config` still overrides. Resolved through a new shared `stormpulse.init.files.default_config_path()` helper that mirrors the existing `_default_creds_dir()` logic in `stormpulse.cli`, so future CLI subcommands that need a config-path default have one source of truth to import from instead of pasting another hardcoded `/etc/stormpulse/stormpulse.toml` constant. Original fix queued 2026-05-28 during the alpha-node sign-off walk; ships with 0.1.10 alongside `run_apply_block`.

## [0.1.9] - 2026-05-26

Fixes the rootless-install footgun where `stormpulse enroll` and `stormpulse init` defaulted `--creds-dir` to `/etc/stormpulse`, regardless of who was running them. On a hardened box (no `stormpulse` system user, no write to `/etc/`), enrollment would sign the CSR successfully, mark the token used on the dashboard, then fail locally with `PermissionError` - burning the token and forcing the operator to issue a fresh one for every retry. See ADR CORE-003 for the rootless posture this is catching up to.

### Fixed

- **`--creds-dir` defaults are now EUID-aware.** Root → `/etc/stormpulse` (legacy system install). Non-root → `$XDG_CONFIG_HOME/stormpulse` (defaulting to `~/.config/stormpulse`). Applies to both `enroll` and `init`. `--creds-dir` still overrides.
- **`stormpulse enroll` no longer burns the token on local write failures.** The CLI now preflights `creds_dir` (creates it at 0o700 if missing, writes and deletes a marker file) before POSTing the CSR. A local-only failure raises before the network call, so the enrollment token is still valid on retry.
- **`stormpulse init` mode auto-detection is now EUID-based, not probe-based.** Previously `detect_mode()` probed `$XDG_RUNTIME_DIR/docker.sock` to decide between user and system mode. On fresh hardened boxes the probe races the install flow -- rootless dockerd isn't necessarily up yet -- so it would return SYSTEM and `validate_mode_for_euid` would then reject the install with "needs root". Detection now keys off `os.geteuid()` alone: root → SYSTEM, non-root → USER. `--user` / `--system` still override. Matches the `--creds-dir` fix above.

### Added

- **`stormpulse.enroll.preflight_creds_dir()`**. Public helper that validates a credentials directory is writable. Raises `EnrollError` with an actionable hint pointing at `--creds-dir`.
- **`stormpulse init` resolves missing and unowned project directories.** Previously the wizard re-prompted with "Directory not found" on missing paths and silently accepted root-owned dirs (the `sudo mkdir` without `chown` papercut). Now the wizard handles three cases. (1) Path exists and is writable → use it. (2) Path exists but isn't writable by the current user → print `sudo chown -R $USER:$USER <path>` and a "Press Enter once `<path>` is yours, or type a different path" prompt defaulting to the same path. (3) Path doesn't exist → check the deepest existing ancestor: writable ancestor → "Create it? [Y/n]" → `mkdir -p` + report owner; non-writable ancestor → skip the no-op confirm, print `sudo mkdir -p ... && sudo chown $USER:$USER ...`, then the same Press-Enter retry. Operators keep their typed path across the sudo round-trip; no subprocess `sudo`, no escalation surface.
- **`stormpulse init` offers to scaffold a placeholder `docker-compose.yml` when none is found.** On agent-first installs the compose file often doesn't exist yet (the operator is wiring the agent before installing the project's docker stack). When `detect_compose_files` returns nothing *and* the project dir is writable by the current user, the wizard now prompts: "Scaffold a placeholder at `<project_dir>/docker-compose.yml`?" - on yes it writes a minimal file with one service block (name defaults to `project_dir.name`) using `image: placeholder:latest`. The image is deliberately non-resolvable so `docker compose up` fails loudly until the operator replaces it. If the project dir isn't writable the offer is suppressed and the wizard falls through to manual entry. No escalation, never runs sudo.
- **`stormpulse init` offers to create an empty `.env` when none is found.** Same agent-first-install pattern: the project's `.env` often doesn't exist yet because secrets get added later. When `<project_dir>/.env` is missing *and* the project dir is writable, the wizard prompts "Create empty `<path>`? [Y/n]" - on yes it `touch`es the file and reports the owner, so the wizard can move on without the operator switching shells. Decline or unwritable dir falls through to the existing manual-entry/skip flow. The existing `skip` default at the manual prompt is preserved as the no-action escape.
- **`stormpulse init` remembers the pulse token across re-runs.** Operators iterating on init (because a downstream prompt broke, or to tweak one answer) had to re-find and re-type the pulse token from the dashboard every time. The wizard now reads `[agent].pulse_token` from any prior `stormpulse.toml` at the install's expected location and offers it as the prompt default - Enter keeps it, typing a new UUID replaces it. The remembered token is validated against the same UUID format the prompt enforces, so a stale or hand-edited entry that wouldn't pass anyway is silently skipped (no broken default). No new files written; reads the same config the wizard would overwrite anyway.
- **New `stormpulse update` subcommand wraps the canonical pipx reinstall.** The documented update flow was two commands (`pipx upgrade ...` followed by a `systemctl --user restart`), and the first one was a quiet footgun: this project lands fixes in `main` before bumping `pyproject.toml`, so `pipx upgrade` checks the declared version, finds no change, and tells the operator they're current while they're running stale code. `stormpulse update` instead runs `pipx install --force git+<repo>@<branch>` (default `main`) and then `systemctl --user restart stormpulse`. `--source pip [--version X.Y.Z]` switches to the pypi index for release-cut installs. `--no-restart` skips the restart and prints the command to run when ready (e.g. for batched updates across boxes). In system-mode installs (EUID 0) the restart is also skipped and the operator gets the `systemctl restart stormpulse` line to run - no subprocess `sudo`, matching the no-escalation posture of `enroll` and `init`. `pipx install --force` replaces the on-disk binary but the running process keeps the old code in memory until restart, so auto-restart is the correct default rather than a cosmetic add-on.

## [0.1.8] - 2026-05-26

Adds `run_verify_block`, the dashboard-driven verify hatch that powers the Storm Developments website's sign-off checklist auto-check feature, **and** the operator-owned seal that bounds it. The hatch is wide on purpose - the agent runs whatever HMAC-signed shell the dashboard sends - and the seal is how the operator closes it once onboarding is done so it isn't open forever. See ADR CORE-004.

### Added

- **`run_verify_block` command.** Registered in the standard command registry alongside `git_pull` and `docker_logs`. Template `["/bin/bash", "-c", "{verify_command}"]`; the shell text arrives as a parameter (4 KiB cap, no regex restriction). Group `signoff`, 30s default timeout.
- **Sign-off seal.** `stormpulse signoff seal` writes a flag file in the agent's state directory; the agent then excludes `run_verify_block` from its registry and refuses any inbound dispatch of it, returning `failure_reason="signoff_sealed"`. `stormpulse signoff unseal` removes the flag for re-verification; `stormpulse signoff status` reports the current state. The flag is operator-owned by filesystem permissions - neither the dashboard nor a `run_verify_block` payload can flip it.
- **Dispatch-time recheck.** The seal is re-stat'd on every incoming `command.request` and `command.sequence`, so an operator sealing mid-run takes effect for the next command without an agent restart.
- **Register-payload `signoff_sealed` flag.** Agents that have shipped the seal report current state to the dashboard on every (re)connect, so the dashboard's verify UI can reflect it without a separate poll.
- **`stormpulse.signoff` module.** `SignoffState` and `state_dir_from_db_path`. Co-located with the nonce DB.

### Changed

- **Trust boundary, named and bounded.** This is the first registered command whose shell text travels on the wire (HMAC-signed by the dashboard) rather than being baked into the agent. Older built-ins ship templated shell with operator-supplied parameters filling pre-defined slots (`docker_service_name`, `tail_lines`); `run_verify_block` accepts a full opaque shell string from the dashboard. The seal bounds this in time: while unsealed (the onboarding window) the dashboard can dispatch any verify shell; once sealed (post-onboarding) the registry has the same shape it had pre-0.1.8 and the hatch is gone until the operator unseals on the host. The trust shift is now an explicit, operator-controlled window rather than a perpetual capability. ADR CORE-004 records the rationale and follow-ups (notably optional `bwrap` confinement of the verify shell).

## [0.1.7] - 2026-05-25

Adds rootless / user-mode install so Storm-hardened boxes (rootless docker, no `docker` group) can run the agent without weakening the hardening posture. See ADR CORE-003.

### Added

- **User-mode install.** `stormpulse init` now auto-detects rootless docker via `$XDG_RUNTIME_DIR/docker.sock` and installs as a user systemd unit at `~/.config/systemd/user/stormpulse.service`. Config + creds live under `~/.config/stormpulse/`, data under `~/.local/share/stormpulse/`. The user unit sets `DOCKER_HOST=unix://%t/docker.sock` so the agent's `docker compose` calls reach the per-user rootless dockerd. Force the mode with `--user` or `--system` flags.
- **`stormpulse migrate-to-rootless` subcommand.** Converts an existing system install in-place: stops the system unit via sudo, copies creds from `/etc/stormpulse/` to `~/.config/stormpulse/` and re-chowns to the invoking user, translates the TOML paths, writes the user systemd unit, and starts it. Cryptographic identity is preserved - no re-enrollment. Old install left in place for rollback (`sudo systemctl enable --now stormpulse`); operator removes it manually once the new agent is verified healthy.
- **Linger check.** The init wizard warns if `loginctl enable-linger $USER` is not set, since user units stop at logout without it. Playbook `001-ubuntu-baseline` already enables linger for the admin user.
- **`stormpulse.init.mode`**: `InstallMode` enum (SYSTEM, USER), `detect_mode()`, `resolve_mode()`, `validate_mode_for_euid()`. Public surface for future tooling that needs to know how the agent is installed.

### Changed

- **`stormpulse init` mismatch errors are clearer.** Running `stormpulse init --user` as root now fails with *"Rerun without sudo for user mode. The user systemd unit must be owned by the unprivileged user that runs rootless docker."* Running `stormpulse init` (no flags) on a system without root or rootless docker fails with a message that points at the right resolution (sudo for system, or pass `--user`).
- **`run_init()` signature**: gains an optional `mode: InstallMode | None` parameter (default `None`, which auto-detects). Existing callers that omit it keep working.
- **`render_systemd_unit()` signature**: gains optional `mode`, `agent_bin`, `config_path` keyword arguments. The original positional `project_dir` call still produces the system unit unchanged.
- **`run_system_setup()` signature**: gains optional `mode` parameter. In user mode, skips the docker-group `usermod` and the recursive `root:stormpulse` chown - the agent runs as the operator who already owns the project directory.

## [0.1.6] - 2026-05-18

Adds Storm Pulse's first Caddy integration, enabling per-region custom-domain hosting on regional VPS hosts.

### Added

- **Caddy integration.** New `[caddy]` config section enables Storm Buckets's per-region custom-domain hosting. Run `stormpulse caddy init` to set up. See [Caddy Integration](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Caddy-Integration).
- **Boot-time Caddyfile import check.** The agent refuses to start if the main Caddyfile does not import the configured drop-in path - catches "fragment written but never served" misconfigurations at boot, not weeks later when a customer activation hangs.
- **`caddy_json` parser ships TLS cert events.** Log lines from `tls.*` loggers now pass through with cert-lifecycle fields preserved (`logger`, `msg`, `identifier`, `names`, `error`). Previously: silently dropped.

### Changed

- `ParamDef` now supports `max_bytes` for opaque-content params; declarations must set at least one of `pattern` or `max_bytes`. Affects custom-command authors only if they ship multi-line content params. See [Customize Commands - Parameters](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Customize--Commands#parameters).

## [0.1.5] - 2026-05-18

Adds customer bucket provisioning, alias management, and additional data-plane operations to the Garage integration. Lowers the default manifest cadence to 30 seconds.

### Added

- **Customer bucket provisioning commands.** `garage_provision_customer_bucket`, `garage_delete_provisioned_bucket`, `garage_provision_additional_key`, `garage_rotate_customer_key` - long-running, dispatched by Storm Buckets. See [Garage Integration - Customer bucket provisioning](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Garage-Integration#customer-bucket-provisioning).
- **Data-plane commands.** `garage_bucket_set_cors` (configure CORS rules) and `garage_walk_bucket_stats` (count objects and bytes under a prefix). Long-running, use the SigV4 client introduced in 0.1.4.
- **Alias management commands.** `garage_bucket_alias_global_add`, `garage_bucket_alias_global_remove`, `garage_bucket_alias_local_add`, `garage_bucket_alias_local_remove`. See [Garage Integration - Aliases](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Garage-Integration#aliases).
- **Tiered permission commands.** `garage_bucket_allow_rw` and `garage_bucket_allow_ro` split the `garage_bucket_allow` flow by tier, avoiding a conditional `permissions` parameter.

### Changed

- **Manifest cadence default lowered to 30s** (was 300s). Out-of-band Garage changes now reconcile in ≤30s. Existing installs should edit `state_push_interval_seconds = 30` in `stormpulse.toml`, or re-run `stormpulse garage init`.
- Provisioned bucket key names now use hyphens instead of underscores. Affects newly-provisioned keys only.
- `garage_delete_provisioned_bucket` orchestration now handles local aliases (detaches them before deleting the bucket) and cleans up orphaned keys (deletes keys whose only access was to the deleted bucket).

## [0.1.4] - 2026-05-05

This release introduces a long-running command pattern in the Storm Pulse protocol and ships the first command that uses it: `garage_bucket_clear`. It also extends the agent with a small, auditable SigV4 S3 client (no boto3) for talking to a local Garage data-plane endpoint.

### Added

- **Protocol: long-running command pattern.** New `command.progress` message type and `long_running` boolean on command metadata. Long-running commands emit one or more progress events between the originating `command.request` and the terminal `command.result`. See [Protocol Specification - Long-running commands](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Protocol-Specification#long-running-commands).
- **Agent job manager** (`stormpulse/commands/jobs.py`). Generic asyncio task substrate that any long-running command handler can plug into. Handles spawning, progress emission, terminal-result construction, and cancellation on agent disconnect. One `JobManager` per active WebSocket connection; jobs do not survive reconnects.
- **`garage_bucket_clear` command.** Bulk-deletes every object in a bucket via the local Garage S3 endpoint. Marked `long_running=true` and `sensitive_output=true`. Required params: `bucket_name`, `s3_endpoint`, `region`, `access_key_id`, `secret_access_key`. The customer's secret rides in the signed envelope, lives in agent process memory only for the job's lifetime, and is never persisted, never logged, and never appears in result payloads.
- **Hand-rolled SigV4 S3 client** (`stormpulse/garage/s3.py`). Two operations only - `list_objects_v2` and `delete_objects` - plus a `head_bucket` pre-flight. Built on stdlib + `cryptography` (already a runtime dep). No boto3, no new dependencies. SigV4 implementation verified against the AWS-published `get-vanilla` test vector.
- **Structured failure reasons for `garage_bucket_clear`:** `auth_failed` (HeadBucket rejected creds), `partial_failure` (DeleteObjects reported per-object errors - overall job marked failed; bucket counts left untouched), `os_error` (HTTP-layer failure during list or delete).
- **Top-level extras on `command.result`.** Long-running commands report command-specific summary fields at the top of the payload alongside the standard result fields. `garage_bucket_clear` reports `deleted_count`, `failed_count`, `errors[]` (each `{Key, Code, Message}`, max 10), `duration_seconds`, and `error` on failure.

### Changed

- `make_command_result(...)` now accepts an optional `extras` keyword argument. Extras merge into the wire payload at the top level. Used by long-running commands to deliver per-operation summary fields without inventing a new message type.
- `CommandDef` gains a `long_running: bool = False` field. Existing built-in commands and config-defined custom commands continue to behave identically; the field is opt-in.
- `register` payload's per-command metadata now includes `long_running`. Older agents that don't set it: dashboards should treat the absent field as `false`.
- Versioning rule clarified: new message types added within v1 are *additive but not silently ignored* - current parsers reject unknown types with `ProtocolError`. Deploy dashboard updates before agent updates that emit new message types.

[Unreleased]: https://git.stormdevelopments.ca/official-public/storm-pulse/compare/v0.3.0...HEAD
[0.3.0]: https://git.stormdevelopments.ca/official-public/storm-pulse/compare/v0.2.1...v0.3.0
[0.2.1]: https://git.stormdevelopments.ca/official-public/storm-pulse/compare/v0.2.0...v0.2.1
[0.2.0]: https://git.stormdevelopments.ca/official-public/storm-pulse/compare/v0.1.6...v0.2.0
[0.1.9]: https://git.stormdevelopments.ca/official-public/storm-pulse/compare/v0.1.8...v0.1.9
[0.1.6]: https://git.stormdevelopments.ca/official-public/storm-pulse/compare/v0.1.5...v0.1.6
[0.1.5]: https://git.stormdevelopments.ca/official-public/storm-pulse/compare/v0.1.4...v0.1.5
[0.1.4]: https://git.stormdevelopments.ca/official-public/storm-pulse/releases/tag/v0.1.4
