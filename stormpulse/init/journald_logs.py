"""Offer opt-in journald shipping for systemd services and timers.

Detect units under /etc/systemd/system, including stopped oneshots;
allow manual entry for any others. Probe each journal before saving,
and ask whether to proceed if unreadable or empty.

Run journalctl as the operator, without sudo. Journal access normally
requires systemd-journal or adm membership.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from stormpulse.init import InitError
from stormpulse.init.prompts import prompt, prompt_confirm

_JOURNALCTL_BINARY = "journalctl"
_SYSTEMCTL_BINARY = "systemctl"

# Offer operator-installed units rather than distro units under /usr/lib.
_OPERATOR_UNIT_DIR = "/etc/systemd/system"

# The agent already ships its JSON logs; journal shipping could feed itself.
# Exclude it from suggestions, but allow manual entry.
_SELF_UNITS = frozenset({"stormpulse.service", "stormpulse"})

_DETECT_TIMEOUT_SECONDS = 15
_PROBE_TIMEOUT_SECONDS = 10

# Match config._LOG_NAME_PATTERN so the loader accepts generated group names.
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
    """Strip the unit suffix and sanitize a group name, capped at 50 characters.

    ``my-daemon.service`` -> ``my-daemon``; ``foo.bar.service`` -> ``foo-bar``.
    """
    stem = unit.rsplit(".", 1)[0] if "." in unit else unit
    return _NAME_SAFE_RE.sub("-", stem)[:50] or "unit"


def journalctl_available() -> bool:
    """Check whether journalctl can run on this host."""
    try:
        subprocess.run(
            [_JOURNALCTL_BINARY, "--version"],
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return True


def _run_systemctl(args: list[str]) -> str | None:
    """Return systemctl stdout, or None on launch failure, timeout, or nonzero exit."""
    try:
        result = subprocess.run(
            [_SYSTEMCTL_BINARY, *args],
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=_DETECT_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _unit_files() -> list[str]:
    """List installed services and timers, including stopped oneshots.

    A running-only filter would hide most timer-triggered services.
    """
    out = _run_systemctl(
        [
            "list-unit-files",
            "--type=service",
            "--type=timer",
            "--no-legend",
            "--no-pager",
            "--plain",
        ]
    )
    if out is None:
        return []
    names = []
    for line in out.splitlines():
        parts = line.split()
        # Skip templates like foo@.service; journalctl needs a concrete instance.
        if parts and not parts[0].startswith('.') and '@.' not in parts[0]:
            names.append(parts[0])
    return names


def _operator_installed(units: list[str]) -> list[str]:
    """Select operator-installed units in one systemctl call.

    Use the loaded FragmentPath to account for overrides and symlinks.
    """
    if not units:
        return []
    out = _run_systemctl(["show", "--property=Id", "--property=FragmentPath", *units])
    if out is None:
        return []
    keep, unit_id = [], None
    for line in out.splitlines():
        if line.startswith("Id="):
            unit_id = line[3:].strip()
        elif line.startswith("FragmentPath="):
            path = line[len("FragmentPath=") :].strip()
            if unit_id and path.startswith(_OPERATOR_UNIT_DIR + "/"):
                keep.append(unit_id)
            unit_id = None
    return keep


def detect_candidate_units(configured: set[str]) -> list[str]:
    """Suggest operator-installed units, excluding the agent and configured groups."""
    candidates = [
        u
        for u in _operator_installed(_unit_files())
        if u not in _SELF_UNITS and group_name_for_unit(u) not in configured
    ]
    return sorted(set(candidates))


def _choose_from(candidates: list[str]) -> list[str]:
    """Select candidates by comma-separated numbers; blank selects none."""
    print("\n  Systemd units installed on this box:", file=sys.stderr)
    for i, unit in enumerate(candidates, start=1):
        print(f"    {i}. {unit}", file=sys.stderr)
    raw = prompt(
        "  Numbers to ship, comma-separated (blank for none, then type any others)",
    ).strip()
    if not raw:
        return []
    chosen = []
    for token in raw.split(","):
        token = token.strip()
        if not token.isdigit() or not (1 <= int(token) <= len(candidates)):
            print(
                f"  Not one of the numbers offered: {token!r}. Skipped.",
                file=sys.stderr,
            )
            continue
        unit = candidates[int(token) - 1]
        if unit not in chosen:
            chosen.append(unit)
    return chosen


def probe_unit_journal(unit: str) -> str | None:
    """Return None if journal entries are readable, otherwise a problem message.

    Empty journals also return a message; the caller asks whether to proceed.
    """
    try:
        result = subprocess.run(
            [
                _JOURNALCTL_BINARY,
                "--unit",
                unit,
                "--lines",
                "1",
                "--no-pager",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            shell=False,
            check=False,
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


def _existing_group_names(config_path: Path) -> set[str]:
    """Read configured group names; return an empty set if unreadable or malformed."""
    import tomllib

    try:
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return set()
    return {g.get("name") for g in raw.get("log_groups", []) if g.get("name")}


def _has_log_group(config_path: Path, name: str) -> bool:
    return name in _existing_group_names(config_path)


def _append_journald_log_group(config_path: Path, *, name: str, unit: str) -> None:
    if not config_path.is_file():
        raise InitError(f"Config file not found: {config_path}")
    try:
        with open(config_path, "a") as f:
            f.write(_JOURNALD_LOG_GROUP_TEMPLATE.format(name=name, unit=unit))
    except OSError as exc:
        raise InitError(f"Cannot append to {config_path}: {exc}") from exc


def _add_unit(config_path: Path, unit: str) -> bool:
    """Probe and append a unit's group; return True if written.

    Probe both detected and manually entered units: existence does not imply access.
    """
    name = group_name_for_unit(unit)
    if _has_log_group(config_path, name):
        print(f"  A log group named {name!r} already exists. Skipped.", file=sys.stderr)
        return False

    problem = probe_unit_journal(unit)
    if problem is not None:
        # Let the operator distinguish a typo from a service that has not logged yet.
        print(f"  Cannot read {unit}'s journal: {problem}.", file=sys.stderr)
        if not prompt_confirm("  Add it anyway?", default_yes=False):
            return False

    _append_journald_log_group(config_path, name=name, unit=unit)
    print(f"  Added: {name} (journald, unit {unit})", file=sys.stderr)
    return True


def offer_journald_log_groups(config_path: Path) -> bool:
    """Offer journald log groups; return True if any were appended.

    Offer detected units, then accept manual entries until a blank answer.
    Skip setup if journalctl is unavailable or the operator declines.
    """
    if not journalctl_available():
        return False
    if not prompt_confirm(
        "\nShip logs from a systemd unit's journal?",
        default_yes=False,
    ):
        return False

    wrote = False
    # Offer numbered choices only when candidates exist.
    candidates = detect_candidate_units(_existing_group_names(config_path))
    if candidates:
        for unit in _choose_from(candidates):
            if _add_unit(config_path, unit):
                wrote = True

    while True:
        unit = prompt(
            "  Unit name (blank to finish), e.g. my-daemon.service",
        ).strip()
        if not unit:
            return wrote
        if any(c.isspace() for c in unit):
            print("  Unit names cannot contain whitespace. Skipped.", file=sys.stderr)
            continue

        if _add_unit(config_path, unit):
            wrote = True
