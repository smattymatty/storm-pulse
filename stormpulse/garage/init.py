"""Garage init — detect Garage installation and append [garage] to stormpulse.toml."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from stormpulse.init import InitError, _prompt


# ---------------------------------------------------------------------------
# Garage detection
# ---------------------------------------------------------------------------

_GARAGE_CONFIG_SEARCH_PATHS = [
    Path("/opt/garage/garage.toml"),
    Path("/etc/garage/garage.toml"),
    Path("./garage.toml"),
]


def find_garage_config(override: str | None = None) -> Path | None:
    """Find the Garage config file. Returns None if not found."""
    if override:
        p = Path(override)
        return p if p.is_file() else None
    for p in _GARAGE_CONFIG_SEARCH_PATHS:
        if p.is_file():
            return p
    return None


def parse_garage_container_name(compose_path: Path) -> str:
    """Extract container_name for the Garage service from a compose file.

    Uses naive line-by-line parsing (same pattern as init.py's
    parse_service_names). Scans for image: dxflrs/garage, then looks
    for container_name within that service block.

    Returns 'garaged' if not found or parse fails.
    """
    try:
        lines = compose_path.read_text("utf-8").splitlines()
    except OSError:
        return "garaged"

    in_services = False
    in_garage_service = False
    indent_level = 0

    for line in lines:
        stripped = line.rstrip()
        if stripped == "" or stripped.startswith("#"):
            continue

        if not in_services:
            if re.match(r"^services:\s*$", stripped) or stripped == "services:":
                in_services = True
            continue

        # Detect top-level key (end of services block)
        if re.match(r"^\S", stripped):
            break

        # Service-level entry (2-space indent)
        if re.match(r"^  [a-zA-Z0-9_][\w-]*:\s*$", stripped):
            in_garage_service = False
            indent_level = 2
            continue

        # Inside a service — check for garage image
        content = stripped.lstrip()
        current_indent = len(stripped) - len(content)

        if current_indent > indent_level:
            m = re.match(r"image:\s*[\"']?(dxflrs/garage\b)", content)
            if m:
                in_garage_service = True

            if in_garage_service:
                m = re.match(r"container_name:\s*[\"']?([a-zA-Z0-9_-]+)", content)
                if m:
                    return m.group(1)

    return "garaged"


# ---------------------------------------------------------------------------
# TOML section management
# ---------------------------------------------------------------------------


_GARAGE_TOML_TEMPLATE = """\

[garage]
enabled = true
container_name = "{container_name}"
garage_binary = "{garage_binary}"
docker_binary = "{docker_binary}"
config_path = "{config_path}"
state_push_interval_seconds = {state_push_interval_seconds}
"""


def has_garage_section(config_path: Path) -> bool:
    """Check if the TOML file already has a [garage] section."""
    try:
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
        return "garage" in raw
    except (OSError, tomllib.TOMLDecodeError):
        return False


def remove_garage_section(lines: list[str]) -> list[str]:
    """Remove [garage] section from TOML lines (line-based).

    Finds the [garage] header and removes everything until the next
    section header or EOF. Preserves all other content.
    """
    result: list[str] = []
    in_garage = False

    for line in lines:
        stripped = line.strip()
        if stripped == "[garage]":
            in_garage = True
            # Also remove a preceding blank line if it exists
            if result and result[-1].strip() == "":
                result.pop()
            continue

        if in_garage:
            # Check if we hit the next section
            if re.match(r"^\[(?!garage\])", stripped):
                in_garage = False
                result.append(line)
            # Otherwise skip this line (still in garage section)
            continue

        result.append(line)

    return result


def append_garage_section(
    config_path: Path,
    *,
    container_name: str,
    garage_binary: str,
    docker_binary: str,
    garage_config_path: str,
    state_push_interval_seconds: int,
    force: bool = False,
) -> None:
    """Append [garage] section to an existing stormpulse.toml.

    If force=True and a [garage] section already exists, removes
    it first before appending the new one.

    Raises InitError on file errors.
    """
    if not config_path.is_file():
        raise InitError(f"Config file not found: {config_path}")

    if has_garage_section(config_path):
        if not force:
            raise InitError(
                f"[garage] section already exists in {config_path}. "
                f"Use --force to overwrite."
            )
        # Remove existing section
        try:
            lines = config_path.read_text("utf-8").splitlines(keepends=True)
        except OSError as exc:
            raise InitError(f"Cannot read {config_path}: {exc}") from exc
        lines = remove_garage_section(lines)
        try:
            config_path.write_text("".join(lines))
        except OSError as exc:
            raise InitError(f"Cannot write {config_path}: {exc}") from exc

    block = _GARAGE_TOML_TEMPLATE.format(
        container_name=container_name,
        garage_binary=garage_binary,
        docker_binary=docker_binary,
        config_path=garage_config_path,
        state_push_interval_seconds=state_push_interval_seconds,
    )

    try:
        with open(config_path, "a") as f:
            f.write(block)
    except OSError as exc:
        raise InitError(f"Cannot append to {config_path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Interactive prompts
# ---------------------------------------------------------------------------


def prompt_garage_values(
    *,
    container_name: str = "garaged",
    garage_binary: str = "/garage",
    docker_binary: str = "/usr/bin/docker",
    garage_config_path: str = "/opt/garage/garage.toml",
    state_push_interval_seconds: int = 300,
) -> dict[str, str | int]:
    """Prompt user for Garage config values with defaults."""
    container_name = _prompt("Container name", default=container_name)
    garage_binary = _prompt("Garage binary", default=garage_binary)
    docker_binary = _prompt("Docker binary", default=docker_binary)

    while True:
        interval_str = _prompt(
            "State push interval seconds",
            default=str(state_push_interval_seconds),
        )
        try:
            interval = int(interval_str)
            if interval > 0:
                break
            print("  Must be a positive integer", file=sys.stderr)
        except ValueError:
            print("  Must be a positive integer", file=sys.stderr)

    return {
        "container_name": container_name,
        "garage_binary": garage_binary,
        "docker_binary": docker_binary,
        "garage_config_path": garage_config_path,
        "state_push_interval_seconds": interval,
    }


def prompt_confirm(message: str, *, default_yes: bool = True) -> bool:
    """Yes/no prompt. Returns True for yes."""
    hint = "Y/n" if default_yes else "y/N"
    response = _prompt(f"{message} [{hint}]")
    if not response:
        return default_yes
    return response.lower() in ("y", "yes")


# ---------------------------------------------------------------------------
# Service restart
# ---------------------------------------------------------------------------


def restart_stormpulse() -> bool:
    """Restart the stormpulse systemd service. Returns True on success."""
    try:
        subprocess.run(
            ["/usr/bin/systemctl", "restart", "stormpulse"],
            check=True,
            capture_output=True,
        )
        return True
    except FileNotFoundError:
        print("  systemctl not found", file=sys.stderr)
        return False
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace").strip()
        print(f"  Restart failed: {stderr or exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_garage_init(
    config_path: Path,
    *,
    garage_config_override: str | None = None,
    force: bool = False,
) -> None:
    """Public entry point for ``stormpulse garage init``."""
    # Root check
    if os.geteuid() != 0:
        raise InitError(
            "stormpulse garage init must be run as root "
            "(sudo stormpulse garage init)"
        )

    # Find garage config
    garage_config = find_garage_config(garage_config_override)
    if garage_config is None:
        searched = ", ".join(str(p) for p in _GARAGE_CONFIG_SEARCH_PATHS)
        raise InitError(
            f"No Garage installation detected.\n"
            f"Searched: {searched}\n"
            f"Use --garage-config to specify the path."
        )

    print(f"\nGarage installation detected at {garage_config}\n", file=sys.stderr)

    # Check for existing section
    if has_garage_section(config_path) and not force:
        raise InitError(
            f"[garage] section already exists in {config_path}. "
            f"Use --force to overwrite."
        )

    # Detect container name from compose file
    garage_dir = garage_config.parent
    compose_candidates = [
        garage_dir / "docker-compose.yml",
        garage_dir / "docker-compose.yaml",
    ]
    container_name = "garaged"
    for compose_path in compose_candidates:
        if compose_path.is_file():
            container_name = parse_garage_container_name(compose_path)
            if container_name != "garaged":
                print(
                    f"  Container name: {container_name}  "
                    f"(from {compose_path})",
                    file=sys.stderr,
                )
            break
    else:
        print(
            "  No docker-compose.yml found alongside Garage config. "
            "Using defaults.",
            file=sys.stderr,
        )

    # Prompt for values
    values = prompt_garage_values(
        container_name=container_name,
        garage_config_path=str(garage_config),
    )

    # Confirm
    if not prompt_confirm("\nEnable Garage integration?"):
        print("Aborted.", file=sys.stderr)
        return

    # Write
    append_garage_section(
        config_path,
        container_name=str(values["container_name"]),
        garage_binary=str(values["garage_binary"]),
        docker_binary=str(values["docker_binary"]),
        garage_config_path=str(values["garage_config_path"]),
        state_push_interval_seconds=int(values["state_push_interval_seconds"]),
        force=force,
    )
    print(f"\n  [garage] section written to {config_path}", file=sys.stderr)

    # Restart
    if prompt_confirm("Restart stormpulse now?"):
        if restart_stormpulse():
            print("  stormpulse restarted successfully.", file=sys.stderr)
        else:
            print(
                "  Restart failed. Restart manually:\n"
                "    sudo systemctl restart stormpulse",
                file=sys.stderr,
            )
    else:
        print(
            "\n  Restart later with:\n"
            "    sudo systemctl restart stormpulse",
            file=sys.stderr,
        )
