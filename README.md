# Storm Pulse Agent

[![CI](https://git.stormdevelopments.ca/official-public/storm-pulse/actions/workflows/test.yml/badge.svg)](https://git.stormdevelopments.ca/official-public/storm-pulse/actions)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![Typed: mypy strict](https://img.shields.io/badge/mypy-strict-blue.svg)](https://mypy-lang.org/)

Secure server management agent for [Storm Developments](https://stormdevelopments.ca). Connects outbound to a Django dashboard over WebSocket with mTLS, pushes system metrics, and executes whitelisted deploy commands. Zero listening ports.

## How It Works

1. Agent connects **outbound** to the dedicated Pulse transport at
   `wss://pulse.stormdevelopments.ca/ws/pulse/`. Caddy requires and verifies
   the agent's client certificate before proxying the WebSocket to Django.
2. Sends a `register` message (including its available commands list), then pushes metrics every 15s (CPU, memory, disk, load, containers).
3. Dashboard sends HMAC-signed commands. Agent verifies signature, nonce, and expiry before executing.
4. Commands run via `subprocess.run(shell=False)` against a strict whitelist. Custom commands can be added via config with optional overridable parameters, each typed and validated (pattern, byte cap or schema) before anything runs. No shell injection possible.

The envelope is protocol v2. `register` names the versions this agent speaks
and its contract digest, and no loop starts until a `register.ok` picks one of
them; anything else (a `register.ok` without `protocol`, an `error`, silence
for 30 s) is refused and retried with backoff, never downgraded. A frame the
agent cannot accept (a version it does not speak, an unknown type, an
unreadable envelope) is answered with one `error` message carrying the reason
code and the refused type, never the payload, and the socket stays open. The
command MAC is HMAC-SHA256 over the whole envelope as compact canonical JSON
with `payload.hmac` removed, so `id`, `agent_id`, `ts`, `type` and every
payload field are signed; `wire-contract.json` carries golden vectors to check
a signer against.

`register` is the node's whole self-declaration, every field present on every
send (empty is `{}`, `[]` or `""`, never absent): the versions it speaks and
both contract digests, its commands, integrations, log groups and system
inventory, the hatch seal, the kernel `boot_id` and the process start
(`agent_started_at`, ISO-8601 UTC) so a reconnect is told from a reboot and
from a restart, `components` (the agent itself plus whatever each live
integration contributes, `{name, version, digest}`), and `in_flight`, the
request ids of long-running jobs still running, which is `[]` today since no
job outlives a socket. Each integration entry is an `IntegrationReport`
(declared in `wire-contract.json`): its status and state, its
`command_specs_digest` (over the SDK specs, the bytes a grant pins) and
`commands_fenced`, null or the sentence saying why its commands were refused.

A state push is still a full snapshot, and each `integrations.<id>` entry
says how much to trust it: `generation` (climbs by one per push for the life
of the process, never rewinds on a reconnect), `complete` (false when a
listed bucket's `GetBucketInfo` failed in the last full read, so its absence
is a failed read, not a deletion), `last_full_read_at` (ISO-8601 UTC, `""`
before the first) and `changed_ids`, the ids re-read since the last
`metrics.ack` (hinted, diffed, full and post-mutation reads alike; a push that
gets no ack repeats them). Each Garage bucket carries its own `read_at`, the
moment its `GetBucketInfo` returned; a bucket carried forward from the cache
keeps its stamp.

The control plane acknowledges an accepted snapshot with
`metrics.ack {"push_id": "<metrics.push envelope id>"}`. This releases only the
reads included in that push; newer reads remain pending. Empty or unknown
acknowledgements cannot clear changes. Snapshot transmission continues without
waiting for an ack, and outstanding receipts are bounded.

Events are typed and sequenced. Every (source, kind) the agent emits is
declared once with the fields it may carry; `emit` keeps those and drops the
rest, and the buffer stamps `seq`, monotone per process. The next
`events.batch` carries one `dropped_events` naming what was lost (`dropped`,
buffer overflow; `undeclared`, fields refused). A batch the control plane
refuses comes back as `events.batch.nack` with a reason: the agent drops it
(a malformed batch would be malformed again) and emits one `events_nacked`
with the batch id, the count and the reason, capped at 500 characters.

The command contract is typed end to end. `command.request` params are JSON
values, not strings: a string is held to its `pattern`, an object or array to
its `schema`, either to `max_bytes` (UTF-8 bytes of a string, canonical JSON
of anything else), and `wire-contract.json` names each param's `type`. A
`command.result` carries the command's own figures under `result`, a shape
declared per command in the contract, never merged into the top level, and a
failure carries `error` (`{code, http_status, upstream_code, message}`) beside
`failure_reason`, which is a closed enum: a reason the set does not know is
refused at construction, and a consumer branches on a name, never a stderr
substring. A job still running when the agent is asked to stop gets a terminal
`command.cancelled` (`request_id`, `command`, `reason`) before the socket
closes, so no request id ends in silence. A `command.sequence` is `steps`,
each `{command, params}`, and every step's result echoes the `sequence_id` and
its `step_index`, the three refusals included.

Shipped S3 and Caddy access lines name the operation, never the request. Each
carries `s3_op` (`GetObject`, `PutObject`, `DeleteObjects`, `UploadPart`,
`ListObjectsV2` and the rest of a closed table, `Unknown` otherwise), classified
on the node from the method, the sub-resource and whether a key is present;
the path and any query string (a presigned URL's signature among them) stop
at the parser and never reach `message` either; `bucket` and `object_key`
ride as before, on Garage lines only. The address rides as `peer_ip`, and
every Garage S3 line carries `bucket_id`, `""` when the node cannot resolve
it. `log-line-contract.json` declares the fields of every
parser.

Read the [Protocol Specification](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Protocol-Specification) for exact information.

## Security

Five layers, each independent:

- **Network** -- No inbound ports. Agent initiates all connections.
- **Transport** -- mTLS with per-agent certs from a private CA.
- **Application** -- HMAC-SHA256 + nonce + expiry on every command.
- **Execution** -- Whitelisted commands only. Absolute paths. `shell=False`. Config placeholders from local config only; runtime params are typed and validated against the declared pattern, byte cap or schema.
- **OS** -- Rootless by default: a sudo-less operator user against rootless Docker, no host root, no docker group. Systemd sandboxing. (A legacy system-mode install under a dedicated system user is still supported.)

See the [Security Architecture](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Security-Architecture) wiki page for the full design. Found a vulnerability? [SECURITY.md](SECURITY.md) has the reporting path.

## Setup

Requires Python 3.12+. Three runtime deps: `websockets`, `psutil`, `cryptography`.

Install from PyPI:

```bash
pip install storm-pulse-agent
```

For full setup instructions (operator user, permissions, systemd, firewall), see the [Setup Guide](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Setup-Guide).

Storm Developments uses two deliberately separate endpoints:

| Purpose | Endpoint | Client certificate |
|---------|----------|--------------------|
| One-time enrollment | `https://stormdevelopments.ca/api/enroll/` | Not required; the new agent does not have one yet |
| Agent WebSocket | `wss://pulse.stormdevelopments.ca/ws/pulse/` | Required and verified by Caddy |

Current dashboards return the dedicated WebSocket URL with the enrollment
credentials, and `stormpulse init` uses it as the prompt default. Verify that
the prompt shows `pulse.stormdevelopments.ca`; agents talking to an older
dashboard retain the historical same-host derivation as a compatibility
fallback.

**Install modes.** `stormpulse init` auto-detects which to use:

- **User mode (rootless), the default on hardened boxes.** Runs as a sudo-less operator user against rootless Docker. Config and creds under `~/.config/stormpulse/`, data under `~/.local/share/stormpulse/`, a systemd user unit. No host root, no docker group, no system user.
- **System mode (legacy).** Runs under a dedicated `stormpulse` system user with a system unit; config and creds under `/etc/stormpulse/`. Used only where rootless Docker is not present.

Already on a system install? `stormpulse migrate-to-rootless` converts it in place.

## CLI

```
stormpulse enroll ENDPOINT AGENT_ID TOKEN [--creds-dir DIR] [--force]
stormpulse init [--creds-dir DIR] [--user | --system] [--force]
stormpulse migrate-to-rootless [--force]
stormpulse run [CONFIG]
stormpulse status [CONFIG]
stormpulse signoff status [CONFIG]
stormpulse signoff unseal [CONFIG] [--confirm-hostname HOSTNAME]
stormpulse signoff seal [CONFIG]
stormpulse garage init [--config PATH] [--garage-config PATH] [--force]
stormpulse caddy init [--config PATH] [--force]
stormpulse logging init [--config PATH]
stormpulse update [--source {pip,git}] [--branch BRANCH] [--version VERSION] [--no-restart]
stormpulse --version
```

**enroll** -- One-time enrollment. Generates an EC P-256 keypair, sends a CSR to the dashboard, writes the signed cert + CA cert + HMAC key to the credentials directory (`~/.config/stormpulse/` for a rootless user-mode install, `/etc/stormpulse/` for a legacy system install; override with `--creds-dir`). The private key never leaves the machine.

**init** -- Interactive setup wizard. Auto-detects the install mode (rootless user mode when it finds a rootless Docker socket, legacy system mode otherwise; force with `--user` / `--system`). Generates config, creates the matching systemd unit (user unit or system unit), sets permissions. Run after enrollment. Auto-detects Garage installations and running Docker containers and offers to enable integration / log shipping.

**migrate-to-rootless** -- Converts an existing legacy system install to rootless user mode in place. Preserves the agent's cryptographic identity so the dashboard sees the same agent. Use `--force` to overwrite user-mode files left by a previous migration.

**run** -- Starts the agent. Connects to the dashboard, sends heartbeats and metrics, executes commands. Reconnects automatically with exponential backoff.

**status** -- Local inspection. Shows version, agent ID, config path, dashboard URL, certificate expiry, nonce DB entry count, and whether the agent process is running. No network required.

**signoff status / unseal / seal** -- Manage the verify-block hatch on this host. The agent ships sealed: the dashboard cannot dispatch `run_verify_block` until the operator opens the hatch with `signoff unseal`, which requires typing the host's hostname back at the prompt (or `--confirm-hostname HOSTNAME` for automation). `signoff seal` closes the hatch in one keystroke. The dashboard never gets to seal or unseal: the operator on the host is the only authority. See the [Security Architecture](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Security-Architecture#layer-4-execution) page for the threat model.

**garage init** -- Detects a Garage S3 node and appends a `[garage]` section to an existing `stormpulse.toml`. Auto-detects container name from docker-compose.yml. Use `--force` to overwrite an existing `[garage]` section.

**caddy init** -- Detects a Caddy reverse proxy and appends a `[caddy]` section to the agent config. Sanity-checks the Caddyfile for a Pulse-managed drop-in `import` line and parses TLS cert lifecycle events out of the Caddy admin API. Use `--force` to overwrite an existing `[caddy]` section.

**logging init** -- Detects running Docker containers and appends `[[log_groups]]` blocks for each, using `source_type = "docker_stream"` and the `docker_raw` parser. Skips containers already present in the config. See [Log Shipping](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Logging) for details.

**update** -- Reinstalls the agent in place via `pipx install --force`. `--source git` (default) pulls from the official repo, optionally pinned to `--branch`; `--source pip` pulls the published release. `--no-restart` skips the post-install systemctl restart so you can stage the update without bouncing the agent.

## Configuration

Run `stormpulse init` to generate a config interactively - see the [Setup Guide](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Setup-Guide). Key settings:

| Section | Field | Description |
|---------|-------|-------------|
| `agent` | `id` | Unique identifier for this server |
| `agent` | `pulse_token` | UUID from the Server record in the dashboard |
| `agent` | `disabled_commands` | List of command names to remove from the registry (optional) |
| `dashboard` | `url` | Agent WebSocket URL. Storm Developments installs use `wss://pulse.stormdevelopments.ca/ws/pulse/`. |
| `project` | `project_dir` | Absolute path to the deployed project |
| `project` | `compose_file` | Absolute path to docker-compose.yml |
| `project` | `env_file` | Absolute path to `.env` file (optional, passed as `--env-file` to docker compose) |
| `commands.*` | | Custom commands (optional, see example config) |
| `garage` | `enabled` | Enable Garage S3 integration (optional, default: absent) |
| `garage` | `container_name` | Docker container name for Garage (e.g. `garaged`) |
| `garage` | `config_path` | Path to Garage config file |
| `garage` | `hint_file` | Absolute path to a JSON file, owned by the agent's user, that names bucket ids to re-read on the next push; with it set, a `ListBuckets` membership diff runs every minute and every bucket is re-read every five minutes instead of every push (optional; unset keeps the walk on every push) |

## Documentation

- [Setup Guide](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Setup-Guide) -- Full install, enrollment, systemd, permissions
- [Customize Commands](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Customize--Commands) -- How to disable existing commands, or whitelist new commands
- [Log Shipping](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Logging) -- Tailing container and file logs to the dashboard
- [Garage Integration](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Garage-Integration) -- Garage S3 node management
- [Protocol Specification](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Protocol-Specification) -- Message formats, envelope structure, versioning
- [Security Architecture](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Security-Architecture) -- Threat model, five security layers

## Develop

```bash
git clone https://git.stormdevelopments.ca/official-public/storm-pulse.git && cd storm-pulse
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
make check      # pytest + mypy (strict) + architecture/security invariants
```

`make check` needs no Docker, no network, and no credentials.

### The wire tier

The default suite fakes the systems the agent drives. The wire tier runs
against the real ones in throwaway containers, which is the only way to catch a
dependency release that renames a JSON field, changes a status code, or reworks
an error string.

One directory and one container per Integration, under `tests/wire/<name>/`.
Each owns a `<name>-up` / `test-<name>-wire` pair:

```bash
make garage-up           # digest-pinned Garage on loopback (ports 3910/3913)
make test-garage-wire    # ~40 tests, ~12s
make garage-down

make test-wire           # every integration at once (needs every container up)
```

Each harness mints its own credentials on first use, so there is nothing to
configure and no secret to source. It fails loudly, never skips, if the
container is not up.

Testing a Garage upgrade before the fleet takes it:

```bash
GARAGE_IMAGE=dxflrs/garage:v2.4.0 make garage-up && make test-garage-wire
```

### The wire contract

`wire-contract.json` is generated from the live dataclasses (`make
wire-contract`) and `make check` fails when the checked-in copy drifts from
them. Its `messages` section declares every message the agent sends or
receives, each field with its JSON type, nullability and whether it is
required, units carried in the field name (`_bytes`, `_seconds`, `_percent`).
Two digests ride in the file during the protocol overlap: `digest` covers
`integrations` alone and is what a v1 agent advertises on register, while
`contract_digest` covers `integrations`, `messages` and `commands` and is the
v2 value. A `vectors` section, outside both digests, holds a fixed
`command.request` and `command.sequence`, the test key they are signed with
(the byte ramp `00..1f`, never a real key) and the expected `hmac`. Each
`commands.<name>` entry types its params and declares its `result` shape
(`{root, classes}` through the same walker, null where the command returns
none). The `events.batch` entry carries `sources`, `{source: {kind: [fields]}}` straight
from the event registry, so the control plane vendors what each source may
send; Function 16 of `make check` fails on an `emit(` whose kind or field
nobody declared, and the contract check fails when the file and the registry
disagree.

## License

AGPL-3.0 - see [LICENSE](LICENSE).

Copyright (c) 2026 Mathew Storm.
