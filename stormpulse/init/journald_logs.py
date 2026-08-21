"""Offer journald log shipping for systemd-supervised services.

The third shape the wizard can write, after Docker containers and host-native
Caddy. It exists because a systemd service logs to the journal by default, so
shipping it otherwise means giving it a ``StandardError=`` redirect, a log
directory, permissions on that directory and a rotation policy, all to recreate
what the journal already does.

Units are enumerated, but not all of them: a box has hundreds and almost every
one is distro plumbing. The rule that separates them is WHO INSTALLED THE UNIT.
A fragment under ``/etc/systemd/system`` was put there by an operator; one under
``/usr/lib/systemd/system`` came with the distro. That is a mechanism rather
than a list, so it finds whatever this particular box actually runs and there is
no curated set to rot.

**Timers and oneshots are included, and that is the point rather than a
detail.** A long-running daemon can usually be pointed at a file it writes
itself. A oneshot run by a timer cannot: it does not live long enough to own a
log file, it is not a container, and it writes to the journal and nowhere else.
Backup runs, ``unattended-upgrades``, certificate renewals and scrubs are
exactly the units whose silence matters and whose output journald shipping is
the only way to reach. Filtering on ``--state=running`` would hide every one of
them, because they are stopped almost all of the time.

Detection never becomes a cage: the list is offered, and a free-text entry is
always available for a unit the filter did not surface. Opt-in with a NO default
so a Docker-only box is not nagged. And whichever route a unit arrives by, it
gets the check a hand-edited block cannot do: its journal is read before
anything is written, so a typo is caught at setup instead of becoming a group
that ships nothing forever.

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
_SYSTEMCTL_BINARY = "systemctl"

# Where an operator-installed unit's fragment lives. Anything under
# /usr/lib/systemd/system came with the distro and is noise here.
_OPERATOR_UNIT_DIR = "/etc/systemd/system"

# The agent ships its own output already, as structured JSON, through
# ``stormpulse.logging.writer``. A journald group on it would ship a second and
# unstructured copy of the same lines, and every "shipped batch N" line it logs
# would itself become a line to ship. Excluded from the offer; an operator who
# genuinely wants it can still type it at the free-text prompt.
_SELF_UNITS = frozenset({"stormpulse.service", "stormpulse"})

_DETECT_TIMEOUT_SECONDS = 15
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


def _run_systemctl(args: list[str]) -> 'str | None':
    """systemctl stdout, or None when it could not run. Never raises: a box
    without systemd simply offers no candidates and falls back to free text."""
    try:
        result = subprocess.run(
            [_SYSTEMCTL_BINARY, *args],
            capture_output=True, text=True, shell=False, check=False,
            timeout=_DETECT_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _unit_files() -> list[str]:
    """Every installed service and timer unit, whatever its current state.

    ``list-unit-files`` rather than ``list-units --state=running`` on purpose.
    A timer's oneshot is stopped almost always, so a running-only filter hides
    precisely the units journald shipping exists to reach.
    """
    out = _run_systemctl([
        "list-unit-files", "--type=service", "--type=timer",
        "--no-legend", "--no-pager", "--plain",
    ])
    if out is None:
        return []
    names = []
    for line in out.splitlines():
        parts = line.split()
        # A templated unit ("foo@.service") is not addressable as written:
        # journalctl needs a concrete instance. Offering it would produce a
        # group that matches nothing, which is the failure this module exists
        # to prevent, so it is dropped rather than shown.
        if parts and not parts[0].startswith('.') and '@.' not in parts[0]:
            names.append(parts[0])
    return names


def _operator_installed(units: list[str]) -> list[str]:
    """Filter to units whose fragment an operator placed, in one systemctl call.

    ``FragmentPath`` is what systemd actually loaded, so this follows overrides
    and symlinks rather than guessing from a directory listing.
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
            path = line[len("FragmentPath="):].strip()
            if unit_id and path.startswith(_OPERATOR_UNIT_DIR + "/"):
                keep.append(unit_id)
            unit_id = None
    return keep


def detect_candidate_units(configured: set) -> list[str]:
    """Units worth offering: operator-installed, not the agent itself, not
    already configured. Empty is a normal answer, not a failure."""
    candidates = [
        u for u in _operator_installed(_unit_files())
        if u not in _SELF_UNITS and group_name_for_unit(u) not in configured
    ]
    return sorted(set(candidates))


def _choose_from(candidates: list[str]) -> list[str]:
    """Show the candidates and take a comma-separated pick. Blank selects none,
    which leaves the free-text prompt as the way through."""
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
            print(f"  Not one of the numbers offered: {token!r}. Skipped.", file=sys.stderr)
            continue
        unit = candidates[int(token) - 1]
        if unit not in chosen:
            chosen.append(unit)
    return chosen


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


def _existing_group_names(config_path: Path) -> set:
    """Group names already in the config. An unreadable or malformed config
    yields the empty set: detection then offers everything, and the per-unit
    duplicate check still refuses to write a second block for a name that is
    already there."""
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
    """Probe one unit and append its group. True when a block was written.

    Shared by both routes deliberately: a unit picked off the detected list gets
    the SAME journal probe as one typed by hand. Detection says a unit exists,
    never that this user can read it, and those are different questions on a box
    where the agent is not in ``systemd-journal``.
    """
    name = group_name_for_unit(unit)
    if _has_log_group(config_path, name):
        print(f"  A log group named {name!r} already exists. Skipped.", file=sys.stderr)
        return False

    problem = probe_unit_journal(unit)
    if problem is not None:
        # Warn, never refuse. A typo and a not-yet-logging service are
        # indistinguishable from here, and only the operator knows which. A
        # timer's oneshot that has not fired yet is the common benign case.
        print(f"  Cannot read {unit}'s journal: {problem}.", file=sys.stderr)
        if not prompt_confirm("  Add it anyway?", default_yes=False):
            return False

    _append_journald_log_group(config_path, name=name, unit=unit)
    print(f"  Added: {name} (journald, unit {unit})", file=sys.stderr)
    return True


def offer_journald_log_groups(config_path: Path) -> bool:
    """Offer to append journald ``[[log_groups]]`` blocks; True if any was written.

    Two routes in, and the second is what keeps the first from being a cage:
    operator-installed units are detected and offered by number, then the
    free-text loop takes anything the filter did not surface. Detection finding
    nothing is not a failure, it just means every unit arrives by typing.

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
    # Only when there is something to show. An empty list is the normal answer
    # on a box with no operator-installed units, and printing an empty heading
    # then asking for numbers would be a question with no possible answer.
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
