"""Storm Pulse init — interactive setup wizard after enrollment."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from cryptography import x509
from cryptography.x509.oid import NameOID


class InitError(Exception):
    """Raised when init fails."""


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


# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------


def check_root() -> None:
    """Verify running as root. Raises InitError if not."""
    if os.geteuid() != 0:
        raise InitError(
            "stormpulse init must be run as root (sudo stormpulse init)"
        )


_REQUIRED_CRED_FILES = ("agent.pem", "agent-key.pem", "ca.pem", "hmac.key")


def check_credentials(creds_dir: Path) -> None:
    """Verify all credential files from enrollment exist."""
    if not creds_dir.is_dir():
        raise InitError(
            f"Credentials directory not found: {creds_dir}. "
            f"Run 'stormpulse enroll' first."
        )
    missing = [f for f in _REQUIRED_CRED_FILES if not (creds_dir / f).is_file()]
    if missing:
        raise InitError(
            f"Missing credential files in {creds_dir}: {', '.join(missing)}. "
            f"Run 'stormpulse enroll' first."
        )


def extract_agent_id(creds_dir: Path) -> str:
    """Extract agent ID (CN) from the enrolled certificate."""
    cert_path = creds_dir / "agent.pem"
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if not attrs:
            raise InitError(f"Certificate {cert_path} has no CN attribute")
        return str(attrs[0].value)
    except InitError:
        raise
    except Exception as exc:
        raise InitError(f"Cannot read agent ID from {cert_path}: {exc}") from exc


def load_enroll_metadata(creds_dir: Path) -> dict[str, str]:
    """Load enroll.json written by ``stormpulse enroll``.

    Returns an empty dict if the file is missing or unreadable.
    """
    path = creds_dir / "enroll.json"
    try:
        return dict(json.loads(path.read_text("utf-8")))
    except Exception:  # noqa: BLE001
        return {}


def derive_dashboard_url(endpoint: str) -> str:
    """Derive a default WebSocket dashboard URL from the enrollment endpoint.

    ``https://example.com/api/enroll/`` → ``wss://example.com/ws/pulse/``
    ``http://localhost:8000/api/enroll/`` → ``ws://localhost:8000/ws/pulse/``
    """
    parsed = urlparse(endpoint)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    host = parsed.hostname or ""
    port = parsed.port
    if port and port not in (80, 443):
        netloc = f"{host}:{port}"
    else:
        netloc = host
    return f"{scheme}://{netloc}/ws/pulse/"


# ---------------------------------------------------------------------------
# Compose file detection and parsing
# ---------------------------------------------------------------------------


def detect_compose_files(project_dir: Path) -> list[Path]:
    """Find candidate docker-compose files in a project directory."""
    candidates = [
        project_dir / "docker-compose.yml",
        project_dir / "docker-compose.yaml",
        project_dir / "docker" / "docker-compose.yml",
        project_dir / "docker" / "docker-compose.yaml",
        project_dir / "docker" / "docker-compose.prod.yml",
        project_dir / "docker" / "docker-compose.prod.yaml",
    ]
    return [p for p in candidates if p.is_file()]


def parse_service_names(compose_path: Path) -> list[str]:
    """Parse service names from a compose file (naive line-by-line).

    Looks for a top-level ``services:`` key, then collects lines with
    exactly 2-space indentation followed by a word and colon.
    """
    try:
        lines = compose_path.read_text("utf-8").splitlines()
    except OSError:
        return []

    in_services = False
    services: list[str] = []
    for line in lines:
        stripped = line.rstrip()
        if stripped == "" or stripped.startswith("#"):
            continue
        if not in_services:
            if re.match(r"^services:\s*$", stripped) or stripped == "services:":
                in_services = True
            continue
        # Inside services block
        if re.match(r"^\S", stripped):
            # Hit next top-level key, stop
            break
        m = re.match(r"^  ([a-zA-Z0-9_][\w-]*):\s*$", stripped)
        if m:
            services.append(m.group(1))
    return services


def parse_volume_mounts(compose_path: Path, project_dir: Path) -> list[Path] | None:
    """Parse bind-mount volume directories from a compose file.

    Returns absolute paths for ``./relative:/container`` style mounts.
    Named volumes (no ``./`` prefix) are ignored.

    Returns ``None`` on parse failure (file unreadable or not a compose file)
    so callers can distinguish "no bind mounts" from "couldn't parse."
    """
    try:
        lines = compose_path.read_text("utf-8").splitlines()
    except OSError:
        return None

    # Sanity check: must have a top-level services: line
    if not any(re.match(r"^services:\s*$", line.rstrip()) for line in lines):
        return None

    volumes: list[Path] = []
    in_volumes = False
    for line in lines:
        stripped = line.rstrip()
        if stripped == "" or stripped.startswith("#"):
            continue
        # Detect a volumes: block (at any service-level indentation)
        if re.match(r"^\s+volumes:\s*$", stripped):
            in_volumes = True
            continue
        if in_volumes:
            # Volume list items start with "- " after indentation
            m = re.match(r"""^\s+- ["']?(\./[^:"']+)["']?:""", stripped)
            if m:
                host_path = m.group(1)
                resolved = (project_dir / host_path).resolve()
                if resolved not in volumes:
                    volumes.append(resolved)
                continue
            # If line is not a list item, check if we left the volumes block
            if re.match(r"^\s+\S", stripped) and not stripped.lstrip().startswith("- "):
                in_volumes = False
    return volumes


# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------


def _prompt(message: str, *, default: str | None = None) -> str:
    """Print a prompt to stderr and read from stdin. Raises InitError on EOF."""
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{message}{suffix}: ").strip()
    except EOFError as exc:
        raise InitError("Input ended unexpectedly (EOF)") from exc
    return value if value else (default or "")


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def prompt_pulse_token() -> str:
    """Prompt for the pulse token (UUID format)."""
    while True:
        value = _prompt("Pulse token (from dashboard)")
        if _UUID_RE.match(value):
            return value
        print("  Invalid format — expected a UUID (e.g. a1b2c3d4-5678-...)", file=sys.stderr)


def prompt_dashboard_url(default: str | None = None) -> str:
    """Prompt for the dashboard WebSocket URL."""
    while True:
        value = _prompt("Dashboard WebSocket URL", default=default)
        if value.startswith("wss://") or value.startswith("ws://"):
            if value.startswith("ws://"):
                print("  Warning: ws:// is unencrypted. Use wss:// in production.", file=sys.stderr)
            return value
        print("  URL must start with wss:// or ws://", file=sys.stderr)


def prompt_project_dir() -> Path:
    """Prompt for the project directory."""
    default = str(Path.cwd())
    while True:
        value = _prompt("Project directory", default=default)
        p = Path(value)
        if p.is_dir():
            return p.resolve()
        print(f"  Directory not found: {value}", file=sys.stderr)


def prompt_compose_file(project_dir: Path) -> Path:
    """Auto-detect compose files, let user pick or enter manually."""
    found = detect_compose_files(project_dir)
    if len(found) == 1:
        confirm = _prompt(f"Compose file: {found[0]}? (y/n)", default="y")
        if confirm.lower() in ("y", "yes", ""):
            return found[0]
    elif len(found) > 1:
        print("  Found multiple compose files:", file=sys.stderr)
        for i, p in enumerate(found, 1):
            print(f"    {i}. {p}", file=sys.stderr)
        while True:
            choice = _prompt(f"Pick 1-{len(found)}, or enter a path")
            if choice.isdigit() and 1 <= int(choice) <= len(found):
                return found[int(choice) - 1]
            p = Path(choice)
            if p.is_file():
                return p.resolve()
            print(f"  Not found: {choice}", file=sys.stderr)

    # No auto-detect or user declined
    while True:
        value = _prompt("Path to docker-compose file")
        p = Path(value)
        if p.is_file():
            return p.resolve()
        print(f"  File not found: {value}", file=sys.stderr)


def prompt_docker_service(compose_path: Path) -> str:
    """Parse services from compose file, let user pick the default."""
    services = parse_service_names(compose_path)
    if services:
        print("  Services found:", file=sys.stderr)
        for i, name in enumerate(services, 1):
            print(f"    {i}. {name}", file=sys.stderr)
        default = services[0]
        while True:
            choice = _prompt(
                "Default service for commands (e.g. docker_logs)", default=default,
            )
            if choice.isdigit() and 1 <= int(choice) <= len(services):
                return services[int(choice) - 1]
            if choice:
                return choice
    # No services parsed, manual entry
    while True:
        value = _prompt("Default service for commands (e.g. web)")
        if value:
            return value
        print("  Service name cannot be empty", file=sys.stderr)


def prompt_env_file(project_dir: Path) -> Path | None:
    """Detect .env file, offer to use it or skip."""
    env_path = project_dir / ".env"
    if env_path.is_file():
        choice = _prompt(f"Found {env_path}. Use as env_file? (y/n/skip)", default="y")
        if choice.lower() in ("y", "yes", ""):
            return env_path
        if choice.lower() in ("n", "no", "skip", "s"):
            return None
    choice = _prompt("Path to .env file (or 'skip')", default="skip")
    if choice.lower() in ("skip", "s", ""):
        return None
    p = Path(choice)
    if p.is_file():
        return p.resolve()
    print(f"  Warning: {choice} not found. Writing path anyway.", file=sys.stderr)
    return Path(choice).resolve()


# ---------------------------------------------------------------------------
# File generation
# ---------------------------------------------------------------------------


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


def _write_file(path: Path, data: bytes, mode: int) -> None:
    """Write data atomically with correct permissions."""
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_bytes(data)
        os.chmod(tmp, mode)
        tmp.rename(path)
    except PermissionError as exc:
        tmp.unlink(missing_ok=True)
        raise InitError(
            f"Permission denied writing {path}. Run with sudo."
        ) from exc
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise InitError(f"Failed to write {path}: {exc}") from exc


_CONFIG_PATH = Path("/etc/stormpulse/stormpulse.toml")
_SYSTEMD_PATH = Path("/etc/systemd/system/stormpulse.service")


def write_config_file(path: Path, content: str, *, force: bool = False) -> None:
    """Write the TOML config with mode 0o640, owned by root:stormpulse."""
    if path.is_file() and not force:
        raise InitError(
            f"{path} already exists. Use --force to overwrite."
        )
    _write_file(path, content.encode("utf-8"), 0o640)
    try:
        shutil.chown(path, "root", "stormpulse")
    except (LookupError, PermissionError):
        pass  # stormpulse user/group may not exist yet in test environments


def write_systemd_unit(
    path: Path, project_dir: Path, *, force: bool = False,
) -> None:
    """Write the systemd unit file with mode 0o644."""
    if path.is_file() and not force:
        raise InitError(
            f"{path} already exists. Use --force to overwrite."
        )
    content = _SYSTEMD_UNIT_TEMPLATE.format(project_dir=project_dir)
    _write_file(path, content.encode("utf-8"), 0o644)


# ---------------------------------------------------------------------------
# System setup
# ---------------------------------------------------------------------------


def _run_cmd(args: list[str], *, description: str) -> bool:
    """Run a system command, printing status. Returns True on success."""
    print(f"  {description}...", file=sys.stderr)
    try:
        subprocess.run(args, check=True, capture_output=True)
        return True
    except FileNotFoundError:
        print(f"    Command not found: {args[0]}", file=sys.stderr)
        return False
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace").strip()
        print(f"    Failed: {stderr or exc}", file=sys.stderr)
        return False


def _run_find_apply(
    root: Path,
    exclude: list[Path],
    cmd_args: list[str],
    *,
    description: str,
) -> bool:
    """Run ``find <root> -prune ... -print0 | xargs -0 <cmd>``.

    Excludes directories in *exclude* from traversal using ``-prune``,
    so they are never touched by *cmd_args*.
    """
    print(f"  {description}...", file=sys.stderr)
    find_args: list[str] = ["/usr/bin/find", str(root)]
    for excl in exclude:
        find_args += ["-path", str(excl), "-prune", "-o"]
    find_args += ["-print0"]

    try:
        find_proc = subprocess.Popen(
            find_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        xargs_proc = subprocess.Popen(
            ["/usr/bin/xargs", "-0", *cmd_args],
            stdin=find_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if find_proc.stdout:
            find_proc.stdout.close()
        _, xargs_stderr = xargs_proc.communicate()
        find_proc.wait()
        if find_proc.returncode != 0 or xargs_proc.returncode != 0:
            err = xargs_stderr.decode("utf-8", errors="replace").strip()
            print(f"    Failed: {err or 'non-zero exit'}", file=sys.stderr)
            return False
        return True
    except FileNotFoundError as exc:
        print(f"    Command not found: {exc.filename}", file=sys.stderr)
        return False
    except OSError as exc:
        print(f"    Failed: {exc}", file=sys.stderr)
        return False


def run_system_setup(
    project_dir: Path,
    compose_file: Path,
) -> None:
    """Best-effort system setup: docker group, git safe.directory, permissions."""
    _run_cmd(
        ["/usr/sbin/usermod", "-aG", "docker", "stormpulse"],
        description="Adding stormpulse to docker group",
    )
    _run_cmd(
        ["/usr/bin/git", "config", "--system", "--add", "safe.directory", str(project_dir)],
        description=f"Marking {project_dir} as git safe.directory",
    )

    # Safe recursive chown with volume exclusion
    volume_dirs = parse_volume_mounts(compose_file, project_dir)

    if volume_dirs is None:
        print(
            f"  WARNING: Could not parse {compose_file} for volume mounts.\n"
            f"    Skipping recursive chown to avoid breaking Docker volumes.\n"
            f"    Set ownership manually: chown -R root:stormpulse {project_dir}",
            file=sys.stderr,
        )
    elif volume_dirs:
        if not _run_find_apply(
            project_dir, volume_dirs,
            ["/usr/bin/chown", "root:stormpulse"],
            description=f"chown root:stormpulse {project_dir} (excluding {len(volume_dirs)} volume(s))",
        ):
            print("    Cannot set project directory permissions.", file=sys.stderr)
            return
        _run_find_apply(
            project_dir, volume_dirs,
            ["/usr/bin/chmod", "g+w"],
            description=f"chmod g+w {project_dir} (excluding {len(volume_dirs)} volume(s))",
        )
        existing = [d for d in volume_dirs if d.is_dir()]
        if existing:
            print(f"  Excluded {len(existing)} Docker volume(s) from ownership changes.", file=sys.stderr)
    else:
        if not _run_cmd(
            ["/usr/bin/chown", "-R", "root:stormpulse", str(project_dir)],
            description=f"chown -R root:stormpulse {project_dir}",
        ):
            print("    Cannot set project directory permissions.", file=sys.stderr)
            return
        _run_cmd(
            ["/usr/bin/chmod", "-R", "g+w", str(project_dir)],
            description=f"chmod -R g+w {project_dir}",
        )


def run_daemon_reload() -> None:
    """Reload systemd to pick up the new unit file."""
    if not _run_cmd(
        ["/usr/bin/systemctl", "daemon-reload"],
        description="Reloading systemd",
    ):
        raise InitError("systemctl daemon-reload failed")


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_init(creds_dir: Path, *, force: bool = False) -> None:
    """Public entry point for the init wizard."""
    check_root()
    check_credentials(creds_dir)

    agent_id = extract_agent_id(creds_dir)
    meta = load_enroll_metadata(creds_dir)

    print(f"\nStorm Pulse Init — configuring agent '{agent_id}'\n", file=sys.stderr)

    # Derive dashboard URL default from enrollment metadata
    dashboard_default: str | None = None
    if meta.get("endpoint"):
        dashboard_default = derive_dashboard_url(meta["endpoint"])

    pulse_token = prompt_pulse_token()
    dashboard_url = prompt_dashboard_url(default=dashboard_default)
    project_dir = prompt_project_dir()
    compose_file = prompt_compose_file(project_dir)
    docker_service_name = prompt_docker_service(compose_file)
    env_file = prompt_env_file(project_dir)

    # Garage auto-detection
    garage_section = ""
    print("\nChecking for Garage installation...", file=sys.stderr)
    from stormpulse.garage.init import (
        find_garage_config,
        parse_garage_container_name,
        prompt_confirm,
        prompt_garage_values,
    )
    garage_config = find_garage_config()
    if garage_config:
        print(f"  Found: {garage_config}", file=sys.stderr)
        if prompt_confirm("\nEnable Garage integration?"):
            # Detect container name
            garage_dir = garage_config.parent
            container = "garaged"
            for name in ("docker-compose.yml", "docker-compose.yaml"):
                cp = garage_dir / name
                if cp.is_file():
                    container = parse_garage_container_name(cp)
                    break
            values = prompt_garage_values(
                container_name=container,
                garage_config_path=str(garage_config),
            )
            from stormpulse.garage.init import _GARAGE_TOML_TEMPLATE
            garage_section = _GARAGE_TOML_TEMPLATE.format(
                container_name=values["container_name"],
                garage_binary=values["garage_binary"],
                docker_binary=values["docker_binary"],
                config_path=values["garage_config_path"],
                state_push_interval_seconds=values["state_push_interval_seconds"],
            )
    else:
        print("  No Garage installation found. Skipping.", file=sys.stderr)

    config = InitConfig(
        agent_id=agent_id,
        pulse_token=pulse_token,
        dashboard_url=dashboard_url,
        creds_dir=creds_dir,
        project_dir=project_dir,
        compose_file=compose_file,
        docker_service_name=docker_service_name,
        env_file=env_file,
    )

    # Check for existing config
    if _CONFIG_PATH.is_file() and not force:
        confirm = _prompt(f"{_CONFIG_PATH} already exists. Overwrite? (y/n)", default="n")
        if confirm.lower() not in ("y", "yes"):
            raise InitError("Aborted — config file not overwritten")
        force = True

    toml_content = generate_toml(config) + garage_section
    print("\nWriting files...", file=sys.stderr)
    write_config_file(_CONFIG_PATH, toml_content, force=force)
    print(f"  Config:  {_CONFIG_PATH}", file=sys.stderr)

    write_systemd_unit(_SYSTEMD_PATH, project_dir, force=force)
    print(f"  Systemd: {_SYSTEMD_PATH}", file=sys.stderr)

    print("\nSystem setup...", file=sys.stderr)
    run_system_setup(project_dir, compose_file)
    run_daemon_reload()

    print(f"""
Setup complete!

Next steps:
  1. Set the git remote URL in {project_dir}:
     git -C {project_dir} remote set-url origin <HTTPS_URL>
  2. Start the agent:
     sudo systemctl enable --now stormpulse
  3. Check logs:
     sudo journalctl -u stormpulse -f
""", file=sys.stderr)
