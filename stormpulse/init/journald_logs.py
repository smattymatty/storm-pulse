"""Offer journald log shipping for systemd-supervised services.

The third shape the wizard can write, after Docker containers and host-native
Caddy. It exists because a systemd service logs to the journal by default, so
shipping it otherwise means giving it a ``StandardError=`` redirect, a log
directory, permissions on that directory and a rotation policy, all to recreate
what the journal already does.

Unlike containers, systemd units cannot usefully be enumerated: a box has
hundreds and no generic rule separates the interesting ones. So this asks,
rather than detects, and it is opt-in with a NO default so a Docker-only box is
not nagged. What it does add over a hand-edited block is the check the hand
edit cannot do: it reads the unit's journal before writing anything, so a typo
is caught at setup instead of becoming a group that ships nothing forever.

No-escalation posture, same as ``stormpulse logs``: journalctl is invoked as
the operator, never through sudo. Journal read access normally comes from
membership of ``systemd-journal`` or ``adm``, so a unit this user cannot read
is reported and left to the operator rather than escalated around.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from stormpulse.init import InitError
from stormpulse.init.prompts import prompt, prompt_confirm

_JOURNALCTL_BINARY = "journalctl"
_PROBE_TIMEOUT_SECONDS = 10

# Mirrors config._LOG_NAME_PATTERN. A group name that the loader would refuse
# must never be written: the block would be skipped at load with a warning and
# the operator would believe it was configured.
_NAME_SAFE_RE = re.compile(r"[^a-zA-Z0-9_-]")

_JOURNALD_LOG_GROUP_TEMPLATE = """
[[log_groups]]
name = "{name}"
enabled = true
source_type = "journald"
unit = "{unit}"
filter_contains = ""
parser = "journald"
ship_interval_seconds = 10
max_lines_per_batch = 200
"""


def group_name_for_unit(unit: str) -> str:
    """The log group name for a unit, which is how the dashboard says where a
    line came from: the unit minus its type suffix, with anything the config
    loader would reject replaced by a hyphen.

    ``my-daemon.service`` -> ``my-daemon``; ``foo.bar.service`` -> ``foo-bar``.
    """
    stem = unit.rsplit(".", 1)[0] if "." in unit else unit
    return _NAME_SAFE_RE.sub("-", stem)[:50] or "unit"


def journalctl_available() -> bool:
    """Whether this box has journalctl at all. A non-systemd host is not asked."""
    try:
        subprocess.run(
            [_JOURNALCTL_BINARY, "--version"],
            capture_output=True, text=True, shell=False, check=False,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return True


def probe_unit_journal(unit: str) -> 'str | None':
    """None when the unit's journal is readable, otherwise why it is not.

    A readable journal with no entries yet is NOT an error: a freshly installed
    service has written nothing, and refusing that would be worse than the typo
    this guards against. The caller warns and lets the operator decide.
    """
    try:
        result = subprocess.run(
            [_JOURNALCTL_BINARY, "--unit", unit, "--lines", "1", "--no-pager", "--quiet"],
            capture_output=True, text=True, shell=False, check=False,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return "journalctl is not installed"
    except subprocess.TimeoutExpired:
        return "journalctl timed out"
    if result.returncode != 0:
        return (result.stderr.strip() or f"journalctl exited {result.returncode}")[:200]
    if not result.stdout.strip():
        return "no journal entries yet"
    return None


def _has_log_group(config_path: Path, name: str) -> bool:
    import tomllib

    try:
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return any(g.get("name") == name for g in raw.get("log_groups", []))


def _append_journald_log_group(config_path: Path, *, name: str, unit: str) -> None:
    if not config_path.is_file():
        raise InitError(f"Config file not found: {config_path}")
    try:
        with open(config_path, "a") as f:
            f.write(_JOURNALD_LOG_GROUP_TEMPLATE.format(name=name, unit=unit))
    except OSError as exc:
        raise InitError(f"Cannot append to {config_path}: {exc}") from exc


def offer_journald_log_groups(config_path: Path) -> bool:
    """Offer to append journald ``[[log_groups]]`` blocks; True if any was written.

    Loops so several units can be added in one pass, and stops on a blank
    answer. Silent no-op on a box without journalctl, or when the operator
    declines.
    """
    if not journalctl_available():
        return False
    if not prompt_confirm(
        "\nShip logs from a systemd unit's journal?", default_yes=False,
    ):
        return False

    wrote = False
    while True:
        unit = prompt(
            "  Unit name (blank to finish), e.g. my-daemon.service",
        ).strip()
        if not unit:
            return wrote
        if any(c.isspace() for c in unit):
            print("  Unit names cannot contain whitespace. Skipped.", file=sys.stderr)
            continue

        name = group_name_for_unit(unit)
        if _has_log_group(config_path, name):
            print(f"  A log group named {name!r} already exists. Skipped.", file=sys.stderr)
            continue

        problem = probe_unit_journal(unit)
        if problem is not None:
            # Warn, never refuse. A typo and a not-yet-logging service are
            # indistinguishable from here, and only the operator knows which.
            print(f"  Cannot read {unit}'s journal: {problem}.", file=sys.stderr)
            if not prompt_confirm("  Add it anyway?", default_yes=False):
                continue

        _append_journald_log_group(config_path, name=name, unit=unit)
        print(f"  Added: {name} (journald, unit {unit})", file=sys.stderr)
        wrote = True
