"""Logging init - detect Docker containers and append [[log_groups]] blocks."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from stormpulse.init import InitError, prompt
from stormpulse.init.files import default_config_path
from stormpulse.init.host_native_logs import offer_caddy_log_group
from stormpulse.init.mode import InstallMode, detect_mode
from stormpulse.init.prompts import prompt_confirm
from stormpulse.init.registry import register_init_step
from stormpulse.init.system import restart_or_hint

_DEFAULT_DOCKER_BINARY = "/usr/bin/docker"


_LOG_GROUP_TEMPLATE = """
[[log_groups]]
name = "{name}"
enabled = true
source_type = "docker_stream"
container_name = "{container_name}"
docker_binary = "{docker_binary}"
filter_contains = "{filter_contains}"
parser = "{parser}"
ship_interval_seconds = {ship_interval_seconds}
max_lines_per_batch = 200
retention_days = 90
"""


# Map container-name patterns to (parser, filter_contains).
# First match wins. Patterns match the container name exactly or as a
# whole word inside it (so "my-caddy-1" still matches "caddy").
_CONTAINER_PARSER_HINTS: list[tuple[str, str, str]] = [
    ("caddy", "caddy_json", ""),
    ("garaged", "garage_s3", "garage_api"),
    ("garage", "garage_s3", "garage_api"),
]


def _pick_parser(container_name: str) -> tuple[str, str]:
    """Return (parser, filter_contains) for a container name.

    Falls back to the generic ``docker_raw`` parser with no filter when
    no hint matches.
    """
    name_lower = container_name.lower()
    for needle, parser, filter_contains in _CONTAINER_PARSER_HINTS:
        if needle in name_lower:
            return (parser, filter_contains)
    return ("docker_raw", "")


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

    docker_binary_input = prompt("Docker binary", default=docker_binary)

    while True:
        interval_str = prompt("Ship interval seconds", default="10")
        try:
            interval = int(interval_str)
            # Floor matches config.py's load-time check (2s), so the feed can keep
            # pace with the 2s metrics push. Default stays 10 to keep new installs
            # conservative; tighter is opt-in.
            if interval >= 2:
                break
            print("  Must be >= 2", file=sys.stderr)
        except ValueError:
            print("  Must be a positive integer", file=sys.stderr)

    groups: list[dict[str, str | int]] = []
    for name in enabled:
        parser, filter_contains = _pick_parser(name)
        if parser != "docker_raw":
            print(
                f"  {name}: detected as {parser} (filter={filter_contains!r})",
                file=sys.stderr,
            )
        groups.append(
            {
                "name": name,
                "container_name": name,
                "docker_binary": docker_binary_input,
                "ship_interval_seconds": interval,
                "parser": parser,
                "filter_contains": filter_contains,
            }
        )
    return groups


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
            parser=g.get("parser", "docker_raw"),
            filter_contains=g.get("filter_contains", ""),
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
        str(g["name"])
        for g in groups
        if isinstance(g, dict) and isinstance(g.get("name"), str)
    ]


def run_logging_init(config_path: Path) -> None:
    """Public entry point for ``stormpulse logging init``."""
    # Writability gate. Replaces the old root-only check: USER mode is
    # the default on hardened boxes (ADR core/003).
    if not os.access(config_path.parent, os.W_OK):
        suggested = default_config_path()
        raise InitError(
            f"Cannot write to {config_path}. "
            f"In USER mode the config should be at {suggested} "
            f"and owned by the operator. In SYSTEM mode, re-run with sudo."
        )
    if not config_path.is_file():
        raise InitError(f"Config file not found: {config_path}")

    print("\nChecking for Docker containers...", file=sys.stderr)
    containers = detect_docker_containers()
    wrote_anything = False
    if containers:
        print(
            f"  Found {len(containers)} running container(s): {', '.join(containers)}",
            file=sys.stderr,
        )
        existing = _existing_log_group_names(config_path)
        groups = prompt_logging_setup(containers, existing)
        if groups:
            append_log_groups(config_path, groups)
            for g in groups:
                print(f"  Added: {g['name']} (docker_stream)", file=sys.stderr)
            print(
                f"\n  {len(groups)} log group(s) written to {config_path}",
                file=sys.stderr,
            )
            wrote_anything = True
        else:
            print("  No log groups to add.", file=sys.stderr)
    else:
        print(
            "  No running containers found (or Docker unavailable).",
            file=sys.stderr,
        )

    # Always offer the host-native Caddy log group: a box may have zero
    # Docker containers and still want Caddy access logs shipped.
    if offer_caddy_log_group(config_path):
        wrote_anything = True

    if not wrote_anything:
        return

    # No-escalation posture (see stormpulse.init.system): SYSTEM mode
    # prints the hint and never shells out; USER mode runs the user
    # unit restart after explicit operator consent.
    mode = detect_mode()
    if mode is InstallMode.SYSTEM:
        restart_or_hint(mode)
    elif prompt_confirm("Restart stormpulse now?"):
        restart_or_hint(mode)
    else:
        print(
            "\n  Restart later with:\n    systemctl --user restart stormpulse",
            file=sys.stderr,
        )


def logging_init_step(config_path: Path) -> None:
    """Init step: detect Docker containers, prompt, append [[log_groups]].

    Registered with the init orchestrator (see stormpulse.init.registry).
    ``run_init`` calls this after the base config file is written.
    """
    print("\nChecking for log sources...", file=sys.stderr)
    containers = detect_docker_containers()
    if not containers:
        print("  No running containers found. Skipping.", file=sys.stderr)
        return

    print(
        f"  Found {len(containers)} running container(s): {', '.join(containers)}",
        file=sys.stderr,
    )
    # Read existing group names so re-running init never appends a duplicate block
    # (a duplicate name is now skipped-with-warning at load, but not writing it in
    # the first place is cleaner). The `logging init` command already does this via
    # run_init; this is the framework-step path catching up.
    existing = _existing_log_group_names(config_path)
    log_groups = prompt_logging_setup(containers, existing_groups=existing)
    if not log_groups:
        return

    append_log_groups(config_path, log_groups)
    for g in log_groups:
        # source_type is hardcoded in _LOG_GROUP_TEMPLATE above; if the
        # template changes per-group, switch this to g['source_type'].
        print(f"  Added: {g['name']} (docker_stream)", file=sys.stderr)


register_init_step(logging_init_step)
