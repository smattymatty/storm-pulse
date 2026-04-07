# Storm Pulse Agent

Secure server management agent for [Storm Developments](https://stormdevelopments.ca). Connects outbound to a Django dashboard over WebSocket with mTLS, pushes system metrics, and executes whitelisted deploy commands. Zero listening ports.

## How It Works

1. Agent connects **outbound** to the dashboard. Nginx terminates mTLS.
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
- **OS** -- Dedicated user. Systemd sandboxing.

See the [Security Architecture](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Security-Architecture) wiki page for the full design.

## Setup

Requires Python 3.12+. Three runtime deps: `websockets`, `psutil`, `cryptography`.

For full setup instructions (system user, permissions, systemd, firewall), see the [Setup Guide](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Setup-Guide).

## CLI

```
stormpulse enroll ENDPOINT AGENT_ID TOKEN [--creds-dir DIR] [--force]
stormpulse init [--creds-dir DIR] [--force]
stormpulse run [CONFIG]
stormpulse status [CONFIG]
stormpulse garage init [--config PATH] [--garage-config PATH] [--force]
stormpulse --version
```

**enroll** -- One-time enrollment. Generates an EC P-256 keypair, sends a CSR to the dashboard, writes the signed cert + CA cert + HMAC key to `/etc/stormpulse/`. The private key never leaves the machine.

**init** -- Interactive setup wizard. Generates config, creates systemd service, sets permissions. Run after enrollment. Auto-detects Garage installations and offers to enable integration.

**run** -- Starts the agent. Connects to the dashboard, sends heartbeats and metrics, executes commands. Reconnects automatically with exponential backoff.

**status** -- Local inspection. Shows version, agent ID, config path, dashboard URL, certificate expiry, nonce DB entry count, and whether the agent process is running. No network required.

**garage init** -- Detects a Garage S3 node and appends a `[garage]` section to an existing `stormpulse.toml`. Auto-detects container name from docker-compose.yml. Use `--force` to overwrite an existing `[garage]` section.

## Configuration

See [`config/stormpulse.example.toml`](config/stormpulse.example.toml) for all options. Key settings:

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
| `garage` | `state_push_interval_seconds` | How often to refresh Garage state (default: 300) |

## Documentation

- [Setup Guide](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Setup-Guide) -- Full install, enrollment, systemd, permissions
- [Customize Commands](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Customize--Commands) -- How to disable existing commands, or whitelist new commands
- [Protocol Specification](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Protocol-Specification) -- Message formats, envelope structure, versioning
- [Security Architecture](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Security-Architecture) -- Threat model, five security layers

## Develop

```bash
git clone <repo-url> && cd storm-pulse
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest          # 481 tests
mypy .          # strict
```

## License

MIT
