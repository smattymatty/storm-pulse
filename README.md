# Storm Pulse Agent

Secure server management agent for [Storm Developments](https://stormdevelopments.ca). Connects outbound to a Django dashboard over WebSocket with mTLS, pushes system metrics, and executes whitelisted deploy commands. Zero listening ports.

## How It Works

1. Agent connects outbound to the dashboard. Nginx terminates mTLS.
2. Sends a `register` message, then pushes metrics every 15s (CPU, memory, disk, load, containers).
3. Dashboard sends HMAC-signed commands. Agent verifies signature, nonce, and expiry before executing.
4. Commands run via `subprocess.run(shell=False)` against a strict whitelist. No shell injection possible.

See the [Protocol Specification](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Protocol-Specification) wiki page for message formats and envelope structure.

## Security

Five layers, each independent. See the [Security Architecture](https://git.stormdevelopments.ca/official-public/storm-pulse/wiki/Security-Architecture) wiki page for the full design.

- **Network** -- No inbound ports. Agent initiates all connections.
- **Transport** -- mTLS with per-agent certs from a private CA.
- **Application** -- HMAC-SHA256 + nonce + expiry on every command.
- **Execution** -- Whitelisted commands only. Absolute paths. `shell=False`. Parameters from local config, never from the wire.
- **OS** -- Dedicated user. Systemd sandboxing.

## Install

```bash
python3 -m venv /opt/stormpulse/venv
/opt/stormpulse/venv/bin/pip install .
cp config/stormpulse.example.toml /etc/stormpulse/stormpulse.toml
# edit the config, then:
/opt/stormpulse/venv/bin/stormpulse
```

Requires Python 3.12+. Two runtime deps: `websockets`, `psutil`.

See [`config/stormpulse.example.toml`](config/stormpulse.example.toml) for all options.

## Develop

```bash
git clone <repo-url> && cd storm-pulse
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest          # 177 tests
mypy .          # strict
```

### Structure

```
stormpulse/
    __init__.py        # version
    __main__.py        # CLI entry point
    agent.py           # async WebSocket client + reconnect
    protocol.py        # envelope, payloads, serialization
    config.py          # TOML loading + validation
    auth.py            # HMAC verification + nonce tracking
    metrics.py         # psutil + docker compose collection
    commands/
        registry.py    # whitelist + subprocess execution
        deploy.py      # sequenced deploy runner
```

### Build Phases

1. **Protocol + Config** (done)
2. **Metrics + Commands** (done)
3. **Auth** (done)
4. **Agent loop** (done)
5. **Dashboard** -- Django Channels consumer (separate repo)
6. **PKI + Enrollment** -- step-ca, `stormpulse enroll` CLI

## License

MIT
