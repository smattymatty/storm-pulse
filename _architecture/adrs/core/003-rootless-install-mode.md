---
adr:
  id: "CORE-003"
  title: "Rootless install mode and host-native edge services"
  status: "Accepted"
  date: "2026-05-25"
  tags: ["install", "rootless", "systemd", "docker", "caddy", "fail2ban"]
---

# ADR: Rootless install mode and host-native edge services

**Status:** Accepted

## Context

Storm Developments' `001-ubuntu-baseline` hardening playbook runs Docker rootless and removes the `docker` group. The existing system install assumes both: `usermod -aG docker stormpulse` and `User=stormpulse` in a system unit. Hardened Storm boxes therefore cannot run Pulse without weakening the hardening posture.

Rootless Docker is per-user; the socket at `$XDG_RUNTIME_DIR/docker.sock` is mode 0600 owned by whoever started rootless dockerd. Reaching it from another user means ACLs or running as that user. The latter is simpler and matches mainstream rootless practice.

## Decision

**One install, rootless.** (Amended 2026-10-30: System mode and `migrate-to-rootless` removed in 0.9.0; every node runs rootless.) Pipx install as the operator user that owns rootless Docker, config + creds under `~/.config/stormpulse/`, data under `~/.local/share/stormpulse/`, user systemd unit at `~/.config/systemd/user/stormpulse.service` with `Environment=DOCKER_HOST=unix://%t/docker.sock`. No `User=`, no `ProtectHome=yes`, no docker group, no `stormpulse` system user.

**Root is refused.** Root work is not Storm Pulse's purpose (maintainer, 2026-10-30: "if people want a root based system they can pair pulse with ansible").  `stormpulse`'s `main` checks `os.geteuid()` before it parses an argument and refuses UID 0, so every command refuses: a sudo install, a hand-written root unit or a `sudo stormpulse renew` (which would leave the agent a root-owned key) stops before it writes or opens anything.

## Corollary: edge services on the host

Rootless Docker cannot bind ports below 1024 without lowering `net.ipv4.ip_unprivileged_port_start` or layering NAT. Every public Storm service fronts on 80/443 via Caddy, so either we weaken the hardening or we pull edge services to the host. We pull.

- **Caddy**: `apt install caddy`. The Debian package grants `CAP_NET_BIND_SERVICE` to the binary; systemd unit binds 80/443 directly. Caddyfile at `/etc/caddy/Caddyfile` reverse-proxies to backends on rootless-docker `localhost:<port>`.
- **fail2ban**: `apt install fail2ban`. Tails `/var/log/caddy/access.log`; banaction is `ufw`. No container exec, no log shipping.
- **Backends stay rootless-docker.** Django, Garage, PeerTube, Forgejo, Mastodon bind high ports on `localhost`; Caddy is the only thing publishing 80/443.

### Three log streams

| Stream | Source | Examples |
| --- | --- | --- |
| Server | journald / auditd / syslog | SSH, package updates, kernel |
| Network | `/var/log/caddy/access.log`, fail2ban, ufw | Traffic gate decisions |
| Activity | docker container stdout/stderr | App-level events |

The agent already supports `file`-typed log sources (`stormpulse/config.py:_LOG_SOURCE_TYPES`), so this corollary is documentation + config, not new agent code. The control plane's log view tabs by stream.

## Consequences

**Positive:**

- Hardening playbook stops being incompatible with Pulse.
- Agent's blast radius shrinks: no docker-group membership, can only touch the operator's own containers.
- fail2ban config collapses from container-bridged log shipping to one filter + one ufw action.
- TLS lifecycle (`caddy reload`) decouples from backend container lifecycle.

**Negative:**

- `pipx` is a new prerequisite. One-line `apt install pipx` on Ubuntu 22.04+.
- Caddy version follows Ubuntu's apt repo cadence rather than upstream.

## Governance

**Automated enforcement.** No fitness function. The root refusal is a runtime check (`refuse_root`, `stormpulse/init/checks.py`), pinned per entry point by `tests/test_refuse_root.py`; downstream validation (e.g. dashboard sign-off requiring an active agent) lives in the control plane.

**Manual review.** A future ADR is required to: re-introduce a docker-group dependency on hardened boxes, ship a `--rootless` containerised Caddy variant, or lower `ip_unprivileged_port_start` system-wide.

**Related ADRs:**

- [CORE-000 Internal module architecture](000-internal-module-architecture.md) - the layer topology `stormpulse.init` sits in.
- [CORE-001 Fitness functions](001-fitness-functions.md) - `make fitness` verifies the layer topology holds after the additions.
- [CORE-002 Release pipeline](002-release-and-ci-cd-pipeline.md) - the 0.1.7 entry in `CHANGELOG.md` ships this work.
