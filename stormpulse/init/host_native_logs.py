"""Offer host-native Caddy log shipping (Framework helper, shared by caddy + logging init)."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from stormpulse.init import InitError
from stormpulse.init.prompts import prompt_confirm

DEFAULT_CADDY_ACCESS_LOG = Path("/var/log/caddy/access.log")

_CADDY_LOG_GROUP_TEMPLATE = """\

[[log_groups]]
name = "caddy"
enabled = true
source_type = "file"
source_path = "{path}"
parser = "caddy_json"
ship_interval_seconds = 10
max_lines_per_batch = 200
retention_days = 90
"""


def _has_caddy_log_group(config_path: Path) -> bool:
    try:
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return any(g.get("name") == "caddy" for g in raw.get("log_groups", []))


def _has_caddy_section(config_path: Path) -> bool:
    try:
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return "caddy" in raw


def _append_caddy_log_group(config_path: Path, *, path: str) -> None:
    if not config_path.is_file():
        raise InitError(f"Config file not found: {config_path}")
    try:
        with open(config_path, "a") as f:
            f.write(_CADDY_LOG_GROUP_TEMPLATE.format(path=path))
    except OSError as exc:
        raise InitError(f"Cannot append to {config_path}: {exc}") from exc


def offer_caddy_log_group(config_path: Path) -> bool:
    """Offer to append a file-tailed Caddy log_group block; returns True if appended.

    Triggered by either signal: ``[caddy]`` section in TOML, OR
    ``/var/log/caddy/access.log`` exists on disk. Silent no-op (returns
    False) when the log_group is already configured, neither signal is
    present, or the operator declines.
    """
    if _has_caddy_log_group(config_path):
        return False
    if not (DEFAULT_CADDY_ACCESS_LOG.exists() or _has_caddy_section(config_path)):
        return False
    if not prompt_confirm(
        f"\nShip host-native Caddy access logs from {DEFAULT_CADDY_ACCESS_LOG}?"
    ):
        return False
    _append_caddy_log_group(config_path, path=str(DEFAULT_CADDY_ACCESS_LOG))
    print(
        f"  [[log_groups]] block for caddy written to {config_path}",
        file=sys.stderr,
    )
    return True
