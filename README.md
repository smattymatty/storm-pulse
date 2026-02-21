# Storm Pulse Agent

Secure server management agent for [Storm Developments](https://stormdevelopments.ca) infrastructure. Runs on VPS servers, connects outbound to a Django dashboard over WebSocket with mTLS, executes whitelisted commands, and pushes system metrics. Zero listening ports.

```
                    Django Dashboard (WSS)
                           ^
                           | outbound only
                           | mTLS
                    +------+------+
                    | Storm Pulse |
                    |    Agent    |
                    |  (systemd)  |
                    +-------------+
                       VPS server
```

## How It Works

1. Agent opens an outbound WebSocket connection to the dashboard. Nginx terminates mTLS.
2. On connect, the agent sends a `register` message with its version.
3. Every 15 seconds (configurable), the agent pushes system metrics: CPU, memory, disk, container status, uptime.
4. The dashboard can send commands (`git_pull`, `docker_build`, `docker_down`, `docker_up`, `django_migrate`) individually or as a deploy sequence.
5. Every command is HMAC-signed with a nonce and expiry window. The agent verifies before execution.
6. Commands run via `subprocess.run(shell=False)` against a strict whitelist. No shell injection possible.

## Security

- **Network**: Agent initiates all connections. No inbound ports.
- **Transport**: mTLS with per-agent certificates from a private CA (Smallstep step-ca).
- **Application**: HMAC-SHA256 on every command. Nonce tracking prevents replay. Commands expire after 60 seconds.
- **Execution**: Whitelisted commands only. Absolute binary paths. `shell=False`. Parameters resolved from local config, never from incoming messages.
- **OS**: Dedicated `stormpulse` user. Systemd sandboxing (`ProtectSystem=strict`, `NoNewPrivileges=yes`, `PrivateTmp=yes`).

## For Users

### Requirements

- Python 3.12+
- Two runtime dependencies: `websockets`, `psutil`

### Installation

```bash
python3 -m venv /opt/stormpulse/venv
/opt/stormpulse/venv/bin/pip install .
```

### Configuration

Copy the example config and edit it:

```bash
cp config/stormpulse.example.toml /etc/stormpulse/stormpulse.toml
```

The config has seven sections:

| Section | Purpose |
|---------|---------|
| `[agent]` | Agent identity (`id`) |
| `[dashboard]` | WebSocket URL and reconnect backoff |
| `[tls]` | Paths to CA cert, client cert, and client key |
| `[auth]` | Path to HMAC secret key, command expiry window |
| `[metrics]` | Push interval, whether to collect container stats |
| `[project]` | Project directory, Docker Compose file path, service name |
| `[storage]` | SQLite database path for nonce tracking and metric buffering |

See [`config/stormpulse.example.toml`](config/stormpulse.example.toml) for a complete example with comments.

### Running

```bash
# Directly
/opt/stormpulse/venv/bin/python -m stormpulse

# Or via the entry point
/opt/stormpulse/venv/bin/stormpulse
```

The agent is designed to run as a systemd service. A unit file will be provided in `systemd/stormpulse.service`.

## For Developers

### Setup

```bash
git clone <repo-url> && cd storm-pulse
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Project Structure

```
stormpulse/
    __init__.py          # __version__ (single source of truth)
    __main__.py          # entry point
    protocol.py          # message envelope, payload types, serialization
    config.py            # TOML loading and validation
    auth.py              # mTLS + HMAC verification (Phase 3)
    agent.py             # WebSocket client + reconnect loop (Phase 4)
    metrics.py           # psutil collection (Phase 2)
    commands/
        registry.py      # command whitelist + dispatcher (Phase 2)
        deploy.py        # deploy sequence runner (Phase 2)
tests/
config/
    stormpulse.example.toml
```

### Protocol

All messages are JSON with a common envelope:

```json
{
  "v": 1,
  "type": "heartbeat",
  "id": "uuid4",
  "ts": "2026-02-21T12:00:00Z",
  "agent_id": "vps-toronto-01",
  "payload": {}
}
```

Six message types: `heartbeat`, `metrics.push`, `command.request`, `command.result`, `command.sequence`, `register`.

The `Envelope` stores the payload as a raw `dict`. Consumers parse it into typed dataclasses after matching on `type`:

```python
from stormpulse.protocol import Envelope, MessageType, CommandRequestPayload

envelope = Envelope.from_json(raw_message)
match envelope.type:
    case MessageType.COMMAND_REQUEST:
        req = CommandRequestPayload.from_dict(envelope.payload)
        print(req.command, req.nonce)
```

Factory functions for outbound messages:

```python
from stormpulse.protocol import make_heartbeat, make_metrics_push

heartbeat = make_heartbeat("vps-toronto-01")
ws.send(heartbeat.to_json())
```

### Tests

```bash
pytest                    # 79 tests
mypy .                    # strict on source, check_untyped_defs on tests
```

### Build Phases

1. **Protocol + Config** (done) -- message types, serialization, TOML loading
2. **Metrics + Commands** -- psutil collection, command registry, subprocess execution
3. **Auth** -- HMAC verification, nonce tracking (SQLite), cert loading
4. **Agent loop** -- WebSocket client, reconnect, message routing
5. **Dashboard side** -- Django Channels consumer (separate repo, proprietary)
6. **PKI + Enrollment** -- step-ca setup, `stormpulse enroll` CLI

## License

MIT
