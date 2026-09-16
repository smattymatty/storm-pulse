"""`stormpulse investigate deploy --init`: propose a subject from a unit file.

CORE-009 decision 11. Composed here, in the Entry layer, and not in the wizard
package, because it joins two Framework siblings that `.importlinter` forbids
from importing each other: `init` owns the operator-installed-unit listing and
`wizard` owns the derivation and the TOML write. `cli` sits above both, so the
join costs no duplicate systemd parser and bends no layer.

The investigation itself stays one-shot and non-interactive (`CONTEXT.md` seals
that, and explicitly avoids "wizard"). This is config authoring, which is a
different job that happens before a run, so nothing here runs a probe.

Why derive rather than ask: the operator typing an install path from memory is
what produced three disagreeing measurements of one box on 2026-09-07. A unit
file already states where its thing lives, in the operator's own reviewed words.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import tomllib
from stormpulse.init.journald_logs import detect_candidate_units
from stormpulse.init.prompts import prompt, prompt_confirm
from stormpulse.wizard.deploy_subject import (
    UNIT_PROPERTIES,
    derive_subject,
    parse_unit_properties,
)
from stormpulse.wizard.toml_edit import claim_section

# The roots a proposal may search. Deliberately a constant and not a question:
# widening what a node may look at is a decision with a blast radius (CORE-009
# decision 4), and it belongs in a reviewed default the operator edits by hand,
# never in a prompt answered quickly while doing something else.
DEFAULT_SEARCH_ROOTS = ("/home/storm", "/opt/storm", "/srv")

_SHOW_TIMEOUT_SECONDS = 10


def _show_unit(unit: str) -> dict[str, str]:
    """`systemctl show` for one unit, as a property mapping. Empty on failure."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, unit from a local listing
            ["systemctl", "show", unit, "--no-pager",
             f"--property={','.join(UNIT_PROPERTIES)}"],
            capture_output=True, text=True, timeout=_SHOW_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}
    return parse_unit_properties(result.stdout)


def candidate_units(config_path: Path) -> list[str]:
    """Operator-installed units with no deploy subject yet.

    Reuses `init.journald_logs.detect_candidate_units`, which already resolves
    `FragmentPath` (so it follows overrides rather than guessing from a
    directory), drops templated units that are not addressable as written, and
    excludes the agent's own. Passing an empty set asks it for everything it
    would offer; the deploy-specific filter is applied here.
    """
    return [
        unit for unit in detect_candidate_units(set())
        if unit.rsplit(".", 1)[0] not in existing_subjects(config_path)
    ]


def existing_subjects(config_path: Path) -> set[str]:
    """Subject names already declared in the file, read directly.

    Deliberately NOT via `load_config`: that validates the whole file, so a
    broken `[agent]` section three tables away would make this report zero
    declared subjects and re-offer units that already have one. Authoring has to
    work on a box whose config is wrong, because that is when it is needed. Any
    read failure yields an empty set, which over-offers rather than silently
    skipping a unit the operator wanted.
    """
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return set()
    deploy = raw.get("investigate", {})
    if not isinstance(deploy, dict):
        return set()
    subjects = deploy.get("deploy", {})
    return set(subjects) if isinstance(subjects, dict) else set()


def run_init(config_path: Path) -> int:
    """Offer the box's units, derive a subject from the pick, write it.

    Returns a process exit code. Every path that does not write says why, and
    none of them writes a partial section: `claim_section` replaces the whole
    table or nothing.
    """
    units = candidate_units(config_path)
    if not units:
        print(
            "No units to offer: every operator-installed unit on this box "
            "either already has a deploy subject or is the agent itself.\n"
            f"Add one by hand in {config_path} under "
            "[investigate.deploy.<subject>].",
            file=sys.stderr,
        )
        return 0

    print("\n  Units installed on this box:", file=sys.stderr)
    for i, unit in enumerate(units, start=1):
        print(f"    {i}. {unit}", file=sys.stderr)
    raw = prompt("  Number to add as a deploy subject (blank to cancel)").strip()
    if not raw:
        return 0
    if not raw.isdigit() or not 1 <= int(raw) <= len(units):
        print(f"Not one of the offered numbers: {raw!r}", file=sys.stderr)
        return 1
    unit = units[int(raw) - 1]

    properties = _show_unit(unit)
    subject = derive_subject(unit, properties, DEFAULT_SEARCH_ROOTS)
    if subject is None:
        # Refusing beats guessing: an invented root produces a probe that
        # reports cleanly about a place nothing was installed, which reads on
        # screen as health (CORE-009 decision 11).
        print(
            f"{unit} does not say where it installs: no absolute "
            f"WorkingDirectory and no absolute ExecStart inside "
            f"{', '.join(DEFAULT_SEARCH_ROOTS)}.\n"
            f"Nothing written. Add the subject by hand in {config_path} if you "
            f"know the path this unit's files live under.",
            file=sys.stderr,
        )
        return 1

    section = f"investigate.deploy.{subject.subject}"
    content: dict[str, object] = {
        "units": list(subject.units),
        "expected_root": subject.expected_root,
        "search_roots": list(subject.search_roots),
    }
    print(f"\n  Proposed [{section}], derived from the unit file:", file=sys.stderr)
    for key, value in content.items():
        print(f"    {key} = {value}", file=sys.stderr)
    print(
        "\n  expected_root comes from the unit itself, so anything found "
        "outside it is a finding rather than a surprise.",
        file=sys.stderr,
    )
    if not prompt_confirm("  Write this section?", default_yes=True):
        return 0

    claim_section(config_path, section, content)
    print(
        f"Wrote [{section}] to {config_path}.\n"
        f"Run: stormpulse investigate deploy",
        file=sys.stderr,
    )
    return 0
