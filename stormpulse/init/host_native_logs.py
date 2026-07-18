"""Offer host-native Caddy log shipping (Framework helper, shared by caddy + logging init)."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from stormpulse.init import InitError
from stormpulse.init.prompts import prompt_confirm

DEFAULT_CADDY_ACCESS_LOG = Path("/var/log/caddy/access.log")
DEFAULT_CADDY_EVENTS_LOG = Path("/var/log/caddy/events.log")

_CADDY_LOG_GROUP_TEMPLATE = """\

[[log_groups]]
name = "caddy"
enabled = true
source_type = "file"
source_path = "{path}"
parser = "caddy_json"
ship_interval_seconds = 10
max_lines_per_batch = 200
"""

_CADDY_EVENTS_LOG_GROUP_TEMPLATE = """\

[[log_groups]]
name = "caddy-events"
enabled = true
source_type = "file"
source_path = "{path}"
parser = "caddy_json"
ship_interval_seconds = 10
max_lines_per_batch = 200
"""


def _has_log_group(config_path: Path, name: str) -> bool:
    try:
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return any(g.get("name") == name for g in raw.get("log_groups", []))


def _has_caddy_log_group(config_path: Path) -> bool:
    return _has_log_group(config_path, "caddy")


def _has_caddy_section(config_path: Path) -> bool:
    try:
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return "caddy" in raw


def _append_log_group(config_path: Path, template: str, *, path: str) -> None:
    if not config_path.is_file():
        raise InitError(f"Config file not found: {config_path}")
    try:
        with open(config_path, "a") as f:
            f.write(template.format(path=path))
    except OSError as exc:
        raise InitError(f"Cannot append to {config_path}: {exc}") from exc


def _append_caddy_log_group(config_path: Path, *, path: str) -> None:
    _append_log_group(config_path, _CADDY_LOG_GROUP_TEMPLATE, path=path)


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


def offer_caddy_events_log_group(config_path: Path) -> bool:
    """Offer to append the Caddy cert-events log_group; returns True if appended.

    Certmagic lifecycle events (``tls.obtain`` etc.) go to Caddy's
    process log, never the access log. Storm's custom-domain ACTIVE
    flip waits on ``cert_obtained``, so a serving node must route those
    loggers to a file and ship it - without this group the dashboard
    sticks on CERTIFICATE PENDING forever while the site serves fine
    (found live, 2026-06-11). The file exists only when the Caddyfile
    has a global ``log cert_events`` block writing ``include tls`` to
    ``events.log``; the 001-01-caddy-setup playbook carries it.

    Same dual-signal trigger and idempotency contract as
    ``offer_caddy_log_group``: ``[caddy]`` section in TOML OR the
    events log on disk; silent no-op when already configured, no
    signal, or the operator declines.
    """
    if _has_log_group(config_path, "caddy-events"):
        return False
    if not (DEFAULT_CADDY_EVENTS_LOG.exists() or _has_caddy_section(config_path)):
        return False
    if not prompt_confirm(
        f"\nShip Caddy certificate lifecycle events from {DEFAULT_CADDY_EVENTS_LOG}?"
        " (needs the cert_events log block in the Caddyfile)"
    ):
        return False
    _append_log_group(
        config_path, _CADDY_EVENTS_LOG_GROUP_TEMPLATE,
        path=str(DEFAULT_CADDY_EVENTS_LOG),
    )
    print(
        f"  [[log_groups]] block for caddy-events written to {config_path}",
        file=sys.stderr,
    )
    return True
