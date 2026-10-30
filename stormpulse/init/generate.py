"""TOML and systemd unit template generation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class InitConfig:
    """Collected configuration from the interactive wizard."""

    agent_id: str
    pulse_token: str
    dashboard_url: str
    creds_dir: Path
    project_dir: Path
    compose_file: Path
    docker_service_name: str
    env_file: Path | None
    data_dir: Path


TOML_TEMPLATE = """\
[agent]
id = "{agent_id}"
pulse_token = "{pulse_token}"

[dashboard]
url = "{dashboard_url}"
reconnect_min_seconds = 3
reconnect_max_seconds = 60
heartbeat_interval_seconds = 30

[tls]
ca_cert = "{creds_dir}/ca.pem"
client_cert = "{creds_dir}/agent.pem"
client_key = "{creds_dir}/agent-key.pem"

[auth]
{hmac_secret_line}
command_max_age_seconds = 60

[metrics]
push_interval_seconds = 15
collect_containers = true

[project]
project_dir = "{project_dir}"
compose_file = "{compose_file}"
docker_service_name = "{docker_service_name}"
{env_file_line}
[storage]
db_path = "{db_path}"
"""


def generate_toml(config: InitConfig) -> str:
    """Generate a valid TOML config string from the wizard answers."""
    env_line = ""
    if config.env_file is not None:
        env_line = f'env_file = "{config.env_file}"\n'
    hmac_line = f'hmac_secret = "{config.creds_dir}/hmac.key"'  # skylos: ignore[SKY-S101] path to the key file, not a secret
    return TOML_TEMPLATE.format(
        agent_id=config.agent_id,
        pulse_token=config.pulse_token,
        dashboard_url=config.dashboard_url,
        creds_dir=config.creds_dir,
        project_dir=config.project_dir,
        compose_file=config.compose_file,
        docker_service_name=config.docker_service_name,
        env_file_line=env_line,
        hmac_secret_line=hmac_line,
        db_path=config.data_dir / "stormpulse.db",
    )


# User unit: runs as whoever invoked `systemctl --user`, so no `User=`, and no
# `ProtectHome=yes` (home holds the agent's own config and data). `%t` is
# $XDG_RUNTIME_DIR, where the rootless dockerd socket lives.
USER_SYSTEMD_UNIT_TEMPLATE = """\
[Unit]
Description=Storm Pulse Agent
After=default.target

[Service]
Type=simple
Environment=DOCKER_HOST=unix://%t/docker.sock
ExecStart={agent_bin} run {config_path}
Restart=always
RestartSec=5
TimeoutStopSec=15
KillMode=mixed

# Sandboxing. PrivateTmp is *not* set. It creates a mount
# namespace that breaks rootless Docker socket access: subprocess
# `docker compose ps` reports "permission denied" connecting to
# unix:///run/user/$UID/docker.sock even though DOCKER_HOST, the
# process UID, and the socket perms are all correct. Rootless user
# services have no elevated privileges to defend, so the security
# tradeoff isn't worth breaking the docker integration.
NoNewPrivileges=yes

[Install]
WantedBy=default.target
"""


def render_systemd_unit(agent_bin: Path, config_path: Path) -> str:
    """Render the user systemd unit.

    ``str.replace`` rather than ``str.format`` so paths containing
    ``{`` or ``}`` can't trigger KeyError or unintended substitution.
    """
    return USER_SYSTEMD_UNIT_TEMPLATE.replace("{agent_bin}", str(agent_bin)).replace(
        "{config_path}", str(config_path)
    )
