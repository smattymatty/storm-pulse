# Storm Pulse Agent

[![CI](https://git.stormdevelopments.ca/official-public/storm-pulse/actions/workflows/test.yml/badge.svg)](https://git.stormdevelopments.ca/official-public/storm-pulse/actions)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE)
[![Typed: mypy strict](https://img.shields.io/badge/mypy-strict-blue.svg)](https://mypy-lang.org/)

Secure server management agent for [Storm Developments](https://stormdevelopments.ca). Connects outbound to a Django dashboard over WebSocket with mTLS, pushes system metrics, and executes whitelisted deploy commands. Zero listening ports.

## How It Works

1. Agent connects **outbound** to the dashboard. Caddy terminates mTLS.
2. Sends a `register` message (including its available commands list), then pushes metrics every 15s (CPU, memory, disk, load, containers).
3. Dashboard sends HMAC-signed commands. Agent verifies signature, nonce, and expiry before executing.
4. Commands run via `subprocess.run(shell=False)` against a strict whitelist. Custom commands can be added via config with optional overridable parameters (regex-validated). No shell injection possible.

Read the [Protocol Specification](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Protocol-Specification) for exact information.

## Security

Five layers, each independent:

- **Network** -- No inbound ports. Agent initiates all connections.
- **Transport** -- mTLS with per-agent certs from a private CA.
- **Application** -- HMAC-SHA256 + nonce + expiry on every command.
- **Execution** -- Whitelisted commands only. Absolute paths. `shell=False`. Config placeholders from local config only; runtime params are regex-validated.
- **OS** -- Rootless by default: a sudo-less operator user against rootless Docker, no host root, no docker group. Systemd sandboxing. (A legacy system-mode install under a dedicated system user is still supported.)

See the [Security Architecture](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Security-Architecture) wiki page for the full design. Found a vulnerability? [SECURITY.md](SECURITY.md) has the reporting path.

## Setup

Requires Python 3.12+. Three runtime deps: `websockets`, `psutil`, `cryptography`.

Install from PyPI:

```bash
pip install storm-pulse-agent
```

For full setup instructions (operator user, permissions, systemd, firewall), see the [Setup Guide](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Setup-Guide).

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

**update** -- Reinstalls the agent in place via `pipx install --force`. `--source pip` (default) pulls the published release; `--source git` pulls from the official repo, optionally pinned to `--branch`. `--no-restart` skips the post-install systemctl restart so you can stage the update without bouncing the agent.

## Configuration

Run `stormpulse init` to generate a config interactively - see the [Setup Guide](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Setup-Guide). Key settings:

| Section | Field | Description |
|---------|-------|-------------|
| `agent` | `id` | Unique identifier for this server |
| `agent` | `pulse_token` | UUID from the Server record in the dashboard |
| `agent` | `disabled_commands` | List of command names to remove from the registry (optional) |
| `dashboard` | `url` | WebSocket URL (`wss://...`) |
| `project` | `project_dir` | Absolute path to the deployed project |
| `project` | `compose_file` | Absolute path to docker-compose.yml |
| `project` | `env_file` | Absolute path to `.env` file (optional, passed as `--env-file` to docker compose) |
| `commands.*` | | Custom commands (optional, see example config) |
| `garage` | `enabled` | Enable Garage S3 integration (optional, default: absent) |
| `garage` | `container_name` | Docker container name for Garage (e.g. `garaged`) |
| `garage` | `config_path` | Path to Garage config file |
| `garage` | `state_push_interval_seconds` | How often to refresh Garage state (default: 30) |

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
pytest
mypy .          # strict
make fitness    # architecture + security invariants
```

## License

AGPL-3.0 - see [LICENSE](LICENSE).

Copyright (c) 2026 Mathew Storm.
