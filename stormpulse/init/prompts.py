"""Interactive prompt helpers for ``stormpulse init``."""

from __future__ import annotations

import grp
import pwd
import re
import sys
from pathlib import Path

from stormpulse.init.checks import InitError
from stormpulse.init.compose import detect_compose_files, parse_service_names


def prompt(message: str, *, default: str | None = None) -> str:
    """Print a prompt to stderr and read from stdin. Raises InitError on EOF."""
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{message}{suffix}: ").strip()
    except EOFError as exc:
        raise InitError("Input ended unexpectedly (EOF)") from exc
    return value if value else (default or "")


def prompt_confirm(message: str, *, default_yes: bool = True) -> bool:
    """Yes/no prompt. Returns True for yes."""
    hint = "Y/n" if default_yes else "y/N"
    response = prompt(f"{message} [{hint}]")
    if not response:
        return default_yes
    return response.lower() in ("y", "yes")


UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def prompt_pulse_token() -> str:
    """Prompt for the pulse token (UUID format)."""
    while True:
        value = prompt("Pulse token (from dashboard)")
        if UUID_RE.match(value):
            return value
        print("  Invalid format - expected a UUID (e.g. a1b2c3d4-5678-...)", file=sys.stderr)


def prompt_dashboard_url(default: str | None = None) -> str:
    """Prompt for the dashboard WebSocket URL."""
    while True:
        value = prompt("Dashboard WebSocket URL", default=default)
        if value.startswith("wss://") or value.startswith("ws://"):
            if value.startswith("ws://"):
                print("  Warning: ws:// is unencrypted. Use wss:// in production.", file=sys.stderr)
            return value
        print("  URL must start with wss:// or ws://", file=sys.stderr)


def prompt_project_dir() -> Path:
    """Prompt for the project directory; offer to create it if missing.

    Fresh-box workflow: the operator is often installing the agent
    before the project itself exists. Re-prompting forever made them
    drop the wizard, mkdir in another shell, and start over.

    On a missing path we offer to create it. If the parent isn't
    writable (e.g. ``/opt`` owned by root), we don't escalate -- we
    print the exact ``sudo`` line the operator can paste in another
    shell and re-prompt. Keeps the agent's privilege surface at zero.
    """
    default = str(Path.cwd())
    while True:
        value = prompt("Project directory", default=default)
        p = Path(value)
        if p.is_dir():
            return p.resolve()
        print(f"  Directory {value} does not exist.", file=sys.stderr)
        if not prompt_confirm("  Create it?", default_yes=True):
            continue
        try:
            p.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            print(
                f"  Cannot create {value} -- parent directory not writable.",
                file=sys.stderr,
            )
            print("  Run this in another shell, then retry:", file=sys.stderr)
            print(
                f"    sudo mkdir -p {value} && sudo chown $USER:$USER {value}",
                file=sys.stderr,
            )
            continue
        try:
            st = p.stat()
            owner = pwd.getpwuid(st.st_uid).pw_name
            group = grp.getgrgid(st.st_gid).gr_name
            print(f"  Created {p} (owner: {owner}:{group})", file=sys.stderr)
        except KeyError:
            print(f"  Created {p}", file=sys.stderr)
        return p.resolve()


def prompt_compose_file(project_dir: Path) -> Path:
    """Auto-detect compose files, let user pick or enter manually."""
    found = detect_compose_files(project_dir)
    if len(found) == 1:
        confirm = prompt(f"Compose file: {found[0]}? (y/n)", default="y")
        if confirm.lower() in ("y", "yes", ""):
            return found[0]
    elif len(found) > 1:
        print("  Found multiple compose files:", file=sys.stderr)
        for i, p in enumerate(found, 1):
            print(f"    {i}. {p}", file=sys.stderr)
        while True:
            choice = prompt(f"Pick 1-{len(found)}, or enter a path")
            if choice.isdigit() and 1 <= int(choice) <= len(found):
                return found[int(choice) - 1]
            p = Path(choice)
            if p.is_file():
                return p.resolve()
            print(f"  Not found: {choice}", file=sys.stderr)

    # No auto-detect or user declined
    while True:
        value = prompt("Path to docker-compose file")
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
            choice = prompt(
                "Default service for commands (e.g. docker_logs)", default=default,
            )
            if choice.isdigit() and 1 <= int(choice) <= len(services):
                return services[int(choice) - 1]
            if choice:
                return choice
    # No services parsed, manual entry
    while True:
        value = prompt("Default service for commands (e.g. web)")
        if value:
            return value
        print("  Service name cannot be empty", file=sys.stderr)


def prompt_env_file(project_dir: Path) -> Path | None:
    """Detect .env file, offer to use it or skip."""
    env_path = project_dir / ".env"
    if env_path.is_file():
        choice = prompt(f"Found {env_path}. Use as env_file? (y/n/skip)", default="y")
        if choice.lower() in ("y", "yes", ""):
            return env_path
        if choice.lower() in ("n", "no", "skip", "s"):
            return None
    choice = prompt("Path to .env file (or 'skip')", default="skip")
    if choice.lower() in ("skip", "s", ""):
        return None
    p = Path(choice)
    if p.is_file():
        return p.resolve()
    print(f"  Warning: {choice} not found. Writing path anyway.", file=sys.stderr)
    return Path(choice).resolve()
