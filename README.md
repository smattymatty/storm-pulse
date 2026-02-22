# Storm Pulse Agent

Secure server management agent for [Storm Developments](https://stormdevelopments.ca). Connects outbound to a Django dashboard over WebSocket with mTLS, pushes system metrics, and executes whitelisted deploy commands. Zero listening ports.

## How It Works

1. Agent connects **outbound** to the dashboard. Caddy terminates mTLS.
2. Sends a `register` message, then pushes metrics every 15s (CPU, memory, disk, load, containers).
3. Dashboard sends HMAC-signed commands. Agent verifies signature, nonce, and expiry before executing.
4. Commands run via `subprocess.run(shell=False)` against a strict whitelist. Custom commands can be added via config. No shell injection possible.

## Security

Five layers, each independent:

- **Network** -- No inbound ports. Agent initiates all connections.
- **Transport** -- mTLS with per-agent certs from a private CA.
- **Application** -- HMAC-SHA256 + nonce + expiry on every command.
- **Execution** -- Whitelisted commands only. Absolute paths. `shell=False`. Parameters from local config, never from the wire.
- **OS** -- Dedicated user. Systemd sandboxing.

See the [Security Architecture](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Security-Architecture) wiki page for the full design.

## Quick Start

```bash
# install
python3 -m venv /opt/stormpulse/venv
/opt/stormpulse/venv/bin/pip install .

# enroll (generates keypair locally, sends CSR to dashboard)
stormpulse enroll https://stormdevelopments.ca/api/enroll/ vps-toronto-01 <token>

# configure
cp config/stormpulse.example.toml /etc/stormpulse/stormpulse.toml
# edit stormpulse.toml — set agent.id, agent.pulse_token, project paths

# run
stormpulse run
```

Requires Python 3.12+. Three runtime deps: `websockets`, `psutil`, `cryptography`.

For full setup instructions (system user, permissions, systemd, firewall), see the [Setup Guide](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Setup-Guide).

## CLI

```
stormpulse enroll ENDPOINT AGENT_ID TOKEN [--creds-dir DIR] [--force]
stormpulse run [CONFIG]
stormpulse status [CONFIG]
stormpulse --version
```

**enroll** -- One-time enrollment. Generates an EC P-256 keypair, sends a CSR to the dashboard, writes the signed cert + CA cert + HMAC key to `/etc/stormpulse/`. The private key never leaves the machine.

**run** -- Starts the agent. Connects to the dashboard, sends heartbeats and metrics, executes commands. Reconnects automatically with exponential backoff.

**status** -- Local inspection. Shows version, agent ID, config path, dashboard URL, certificate expiry, nonce DB entry count, and whether the agent process is running. No network required.

## Configuration

See [`config/stormpulse.example.toml`](config/stormpulse.example.toml) for all options. Key settings:

| Section | Field | Description |
|---------|-------|-------------|
| `agent` | `id` | Unique identifier for this server |
| `agent` | `pulse_token` | UUID from the Server record in the dashboard |
| `dashboard` | `url` | WebSocket URL (`wss://...`) |
| `project` | `project_dir` | Absolute path to the deployed project |
| `project` | `compose_file` | Absolute path to docker-compose.yml |
| `project` | `env_file` | Absolute path to `.env` file (optional, passed as `--env-file` to docker compose) |
| `commands.*` | | Custom commands (optional, see example config) |

## Documentation

- [Setup Guide](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Setup-Guide) -- Full install, enrollment, systemd, permissions
- [Protocol Specification](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Protocol-Specification) -- Message formats, envelope structure, versioning
- [Security Architecture](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Security-Architecture) -- Threat model, five security layers

## Develop

```bash
git clone <repo-url> && cd storm-pulse
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest          # 270 tests
mypy .          # strict
```

## License

MIT
