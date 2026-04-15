"""Logging init — detect Docker containers and append [[log_groups]] blocks."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from stormpulse.garage.init import prompt_confirm, restart_stormpulse
from stormpulse.init import InitError, _prompt

_DEFAULT_DOCKER_BINARY = "/usr/bin/docker"


_LOG_GROUP_TEMPLATE = """
[[log_groups]]
name = "{name}"
enabled = true
source_type = "docker"
container_name = "{container_name}"
docker_binary = "{docker_binary}"
filter_contains = ""
parser = "docker_raw"
ship_interval_seconds = {ship_interval_seconds}
max_lines_per_batch = 200
retention_days = 90
"""


def detect_docker_containers(docker_binary: str = _DEFAULT_DOCKER_BINARY) -> list[str]:
    """Return names of currently running Docker containers.

    Returns [] if the docker binary is missing, the daemon is
    unreachable, or the command fails for any reason.
    """
    try:
        result = subprocess.run(
            [docker_binary, "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def prompt_logging_setup(
    containers: list[str],
    existing_groups: list[str],
    *,
    docker_binary: str = _DEFAULT_DOCKER_BINARY,
) -> list[dict[str, str | int]]:
    """Prompt operator for which containers to enable log shipping on.

    Returns a list of ready-to-serialize group dicts. Skips any container
    whose name collides with an existing group (the user can manage
    those manually).
    """
    candidates = [c for c in containers if c not in existing_groups]
    if not candidates:
        print("  All running containers already have log groups.", file=sys.stderr)
        return []

    skipped = [c for c in containers if c in existing_groups]
    for name in skipped:
        print(f"  Skipped: {name} (already configured)", file=sys.stderr)

    enabled: list[str] = []
    if prompt_confirm(f"Enable log shipping for all {len(candidates)} containers?"):
        enabled = list(candidates)
    else:
        for name in candidates:
            if prompt_confirm(f"Enable logs for {name}?"):
                enabled.append(name)

    if not enabled:
        return []

    docker_binary_input = _prompt("Docker binary", default=docker_binary)

    while True:
        interval_str = _prompt("Ship interval seconds", default="10")
        try:
            interval = int(interval_str)
            if interval >= 5:
                break
            print("  Must be >= 5", file=sys.stderr)
        except ValueError:
            print("  Must be a positive integer", file=sys.stderr)

    return [
        {
            "name": name,
            "container_name": name,
            "docker_binary": docker_binary_input,
            "ship_interval_seconds": interval,
        }
        for name in enabled
    ]


def append_log_groups(
    config_path: Path,
    groups: list[dict[str, str | int]],
) -> None:
    """Append `[[log_groups]]` blocks to an existing stormpulse.toml."""
    if not config_path.is_file():
        raise InitError(f"Config file not found: {config_path}")
    if not groups:
        return

    blocks = "".join(
        _LOG_GROUP_TEMPLATE.format(
            name=g["name"],
            container_name=g["container_name"],
            docker_binary=g["docker_binary"],
            ship_interval_seconds=g["ship_interval_seconds"],
        )
        for g in groups
    )
    try:
        with open(config_path, "a") as f:
            f.write(blocks)
    except OSError as exc:
        raise InitError(f"Cannot append to {config_path}: {exc}") from exc


def _existing_log_group_names(config_path: Path) -> list[str]:
    """Best-effort extraction of existing [[log_groups]] names from TOML."""
    try:
        import tomllib
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, Exception):
        return []
    groups = raw.get("log_groups", [])
    if not isinstance(groups, list):
        return []
    return [
        str(g["name"]) for g in groups
        if isinstance(g, dict) and isinstance(g.get("name"), str)
    ]


def run_logging_init(config_path: Path) -> None:
    """Public entry point for ``stormpulse logging init``."""
    if os.geteuid() != 0:
        raise InitError(
            "stormpulse logging init must be run as root "
            "(sudo stormpulse logging init)"
        )
    if not config_path.is_file():
        raise InitError(f"Config file not found: {config_path}")

    print("\nChecking for Docker containers...", file=sys.stderr)
    containers = detect_docker_containers()
    if not containers:
        print(
            "  No running containers found (or Docker unavailable).",
            file=sys.stderr,
        )
        return

    print(
        f"  Found {len(containers)} running container(s): {', '.join(containers)}",
        file=sys.stderr,
    )

    existing = _existing_log_group_names(config_path)
    groups = prompt_logging_setup(containers, existing)
    if not groups:
        print("  No log groups to add.", file=sys.stderr)
        return

    append_log_groups(config_path, groups)
    for g in groups:
        print(f"  Added: {g['name']} (docker)", file=sys.stderr)
    print(f"\n  {len(groups)} log group(s) written to {config_path}", file=sys.stderr)

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
