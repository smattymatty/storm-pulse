"""TOML and systemd unit template generation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from stormpulse.init.mode import InstallMode


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
    # Mode defaults to SYSTEM so existing callers (and the
    # rootful test suite) keep working without code changes.
    mode: InstallMode = InstallMode.SYSTEM
    # User-mode data dir. Ignored in system mode (which uses
    # /opt/stormpulse/data). Set explicitly in user mode so the
    # generated TOML embeds the right path.
    data_dir: Path | None = None


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
    rendered = TOML_TEMPLATE.format(
        agent_id=config.agent_id,
        pulse_token=config.pulse_token,
        dashboard_url=config.dashboard_url,
        creds_dir=config.creds_dir,
        project_dir=config.project_dir,
        compose_file=config.compose_file,
        docker_service_name=config.docker_service_name,
        env_file_line=env_line,
    )
    # In user mode, point [storage].db_path at the user-scoped data
    # location (default ``~/.local/share/stormpulse/stormpulse.db``)
    # instead of the hardcoded /opt/stormpulse/data/stormpulse.db.
    if config.mode is InstallMode.USER and config.data_dir is not None:
        user_db = config.data_dir / "stormpulse.db"
        rendered = rendered.replace(
            'db_path = "/opt/stormpulse/data/stormpulse.db"',
            f'db_path = "{user_db}"',
        )
    return rendered


SYSTEMD_UNIT_TEMPLATE = """\
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
TimeoutStopSec=15
KillMode=mixed

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

# User systemd unit. Runs under the operator's user (whichever user
# invoked `systemctl --user`), so no `User=`/`Group=` directives.
# Drops `ProtectHome=yes` because under a user unit the user's home IS
# the sandbox; locking it out would also lock out the agent's own
# config + data. `Environment=DOCKER_HOST=unix://%t/docker.sock`
# points the docker CLI at the rootless dockerd socket (`%t` is
# systemd's substitution for the unit's runtime dir, i.e.
# $XDG_RUNTIME_DIR). `default.target` is the user-session equivalent
# of multi-user.target.
USER_SYSTEMD_UNIT_TEMPLATE = """\
[Unit]
Description=Storm Pulse Agent (user mode)
After=default.target

[Service]
Type=simple
Environment=DOCKER_HOST=unix://%t/docker.sock
ExecStart={agent_bin} run {config_path}
Restart=always
RestartSec=5
TimeoutStopSec=15
KillMode=mixed

# Sandboxing. Less strict than the system unit because user units
# don't run privileged; the gain of ProtectHome / ProtectSystem is
# marginal and would block access to the agent's own files under ~.
# PrivateTmp is *not* set on the user unit. It creates a mount
# namespace that breaks rootless Docker socket access: subprocess
# `docker compose ps` reports "permission denied" connecting to
# unix:///run/user/$UID/docker.sock even though DOCKER_HOST, the
# process UID, and the socket perms are all correct. Rootless user
# services have no elevated privileges to defend, so the security
# tradeoff isn't worth breaking the docker integration. The system
# unit above keeps PrivateTmp because it runs as a dedicated user
# with broader hardening; that lane doesn't drive rootless Docker.
NoNewPrivileges=yes

[Install]
WantedBy=default.target
"""


def render_systemd_unit(
    project_dir: Path,
    mode: InstallMode = InstallMode.SYSTEM,
    *,
    agent_bin: Path | None = None,
    config_path: Path | None = None,
) -> str:
    """Render a systemd unit string for the requested install mode.

    System mode: substitutes the project_dir into the existing template.
    User mode: substitutes the agent binary path + config path. Both
    use ``str.replace`` rather than ``str.format`` so paths containing
    ``{`` or ``}`` can't trigger KeyError or unintended substitution.
    """
    if mode is InstallMode.USER:
        if agent_bin is None or config_path is None:
            raise ValueError(
                "render_systemd_unit(mode=USER) requires agent_bin and config_path",
            )
        return USER_SYSTEMD_UNIT_TEMPLATE.replace(
            "{agent_bin}", str(agent_bin)
        ).replace("{config_path}", str(config_path))
    return SYSTEMD_UNIT_TEMPLATE.replace("{project_dir}", str(project_dir))
