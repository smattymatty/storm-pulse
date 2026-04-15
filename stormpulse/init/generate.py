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


_TOML_TEMPLATE = """\
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
hmac_secret = "{creds_dir}/hmac.key"
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
db_path = "/opt/stormpulse/data/stormpulse.db"
"""


def generate_toml(config: InitConfig) -> str:
    """Generate a valid TOML config string from the wizard answers."""
    env_line = ""
    if config.env_file is not None:
        env_line = f'env_file = "{config.env_file}"\n'
    return _TOML_TEMPLATE.format(
        agent_id=config.agent_id,
        pulse_token=config.pulse_token,
        dashboard_url=config.dashboard_url,
        creds_dir=config.creds_dir,
        project_dir=config.project_dir,
        compose_file=config.compose_file,
        docker_service_name=config.docker_service_name,
        env_file_line=env_line,
    )


_SYSTEMD_UNIT_TEMPLATE = """\
[Unit]
Description=Storm Pulse Agent
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=stormpulse
Group=stormpulse
Environment=HOME=/opt/stormpulse
ExecStart=/opt/stormpulse/venv/bin/stormpulse run /etc/stormpulse/stormpulse.toml
Restart=always
RestartSec=5

# Sandboxing
ProtectSystem=strict
ReadOnlyPaths=/
ReadWritePaths=/opt/stormpulse/data
ReadWritePaths={project_dir}
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes

[Install]
WantedBy=multi-user.target
"""


def render_systemd_unit(project_dir: Path) -> str:
    """Render the systemd unit with ``project_dir`` substituted.

    Uses ``str.replace`` rather than ``str.format`` so paths containing
    ``{`` or ``}`` can't trigger KeyError or unintended substitution.
    """
    return _SYSTEMD_UNIT_TEMPLATE.replace("{project_dir}", str(project_dir))
