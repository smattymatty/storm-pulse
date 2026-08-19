"""CLI handler for ``stormpulse investigate`` - one-shot diagnostic case files.

An Investigation runs its checks non-interactively and prints a Case file:
one Verdict per suspect (CLEARED / IMPLICATED / INCONCLUSIVE) with its
evidence line, then next moves and named open questions. Guidance lives in
the report's prose, never in prompts (CONTEXT.md: Investigation, Case
file, Verdict). Two doors, one engine: core investigations here, each
Integration's own declared on its descriptor and surfaced as
``stormpulse <id> investigate <name>``.

No self-escalation: a check that cannot see goes INCONCLUSIVE and names
the exact command that would supply the evidence (same posture as
``stormpulse logs``). Every check is split fetch/judge - fetches touch
the host, judges are pure functions over the fetched text, so the verdict
logic is testable without a box.

Receipts: the checks here were field-tested hunting the 2026-07-19 alpha
flap storm; each core investigation's receipt names what it earned.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from stormpulse.sdk.investigate import CaseFile, SuspectReport, Verdict, Window

from .box import run_box
from .flaps import run_flaps
from .logs_pipeline import run_logs_pipeline

# ---------------------------------------------------------------------------
# Window parsing and rendering
# ---------------------------------------------------------------------------

_RELATIVE_RE = re.compile(r"^(?P<n>\d+)(?P<unit>[mhd])$")
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400}


def parse_window(
    since: str | None,
    until: str | None,
    now: datetime,
) -> Window:
    """Resolve operator ``--since``/``--until`` into a concrete Window.

    Accepts relative (``90m``, ``24h``, ``7d``) or absolute
    (``YYYY-MM-DD`` / ``YYYY-MM-DD HH:MM[:SS]``). Default: last 24h.
    Deliberately NOT journalctl's free-text grammar: the window is
    formatted for several evidence sources (journalctl, docker logs,
    log files), so it must be parsed once, here, unambiguously.
    """
    return Window(
        since=_parse_point(since, now) if since else now - timedelta(hours=24),
        until=_parse_point(until, now) if until else None,
    )


def _parse_point(raw: str, now: datetime) -> datetime:
    m = _RELATIVE_RE.match(raw.strip())
    if m is not None:
        return now - timedelta(
            seconds=int(m.group("n")) * _UNIT_SECONDS[m.group("unit")]
        )
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw.strip(), fmt)
        except ValueError:
            continue
    raise SystemExit(
        f"Cannot parse time {raw!r}: use 90m/24h/7d or YYYY-MM-DD [HH:MM]"
    )


_VERDICT_LABEL = {
    Verdict.CLEARED: "CLEARED",
    Verdict.IMPLICATED: "IMPLICATED",
    Verdict.INCONCLUSIVE: "INCONCLUSIVE",
}


def render_case_file(case: CaseFile) -> str:
    """Human-first plain-text rendering. The host owns rendering; an
    investigation only builds the CaseFile."""
    out: list[str] = [
        f"CASE FILE: {case.investigation} - {case.title}",
        f"  Window:  {case.window}",
        f"  Receipt: {case.receipt}",
        "",
        "VERDICTS",
    ]
    for r in case.reports:
        out.append(f"  {_VERDICT_LABEL[r.verdict]:<13} {r.suspect}")
        out.append(f"                {r.evidence}")
        if r.detail:
            out.append(f"                {r.detail}")
        if r.remedy:
            out.append(f"                run: {r.remedy}")
    if case.next_moves:
        out.append("")
        out.append("NEXT MOVES")
        out.extend(f"  - {move}" for move in case.next_moves)
    if case.open_questions:
        out.append("")
        out.append("OPEN QUESTIONS")
        out.extend(f"  - {q}" for q in case.open_questions)
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Core registry + dispatch
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CoreInvestigation:
    title: str
    receipt: str
    run: Callable[[argparse.Namespace, Window], CaseFile]


_CORE: dict[str, _CoreInvestigation] = {
    "flaps": _CoreInvestigation(
        title="agent websocket reconnect churn",
        receipt="earned 2026-07-19: the alpha flap storm - drop taxonomy, "
                "the journald-lag freeze signature, refresh-storm and "
                "shipping-overload rule-outs, all field-tested that day.",
        run=run_flaps,
    ),
    "box": _CoreInvestigation(
        title="host starvation, reboots, upgrades, kernel faults",
        receipt="earned 2026-07-19: the box was frozen by something no "
                "in-guest suspect explained; every check here acquitted "
                "one suspect that day, and scheduled reboots stopped "
                "reading as anomalies.",
        run=run_box,
    ),
    "logs-pipeline": _CoreInvestigation(
        title="per-group shipping health and parser drift",
        receipt="earned 2026-07-19: lines=0 dropped=45 duration=4502ms read "
                "as three alarms and was zero - drain window by design, "
                "drops mostly suppressed-by-design noise. This decides "
                "noise vs format drift with a raw-source sample.",
        run=run_logs_pipeline,
    ),
}


def _case(
    name: str,
    window: Window,
    reports: list[SuspectReport],
    next_moves: list[str],
    open_questions: list[str],
) -> CaseFile:
    core = _CORE[name]
    return CaseFile(
        investigation=name,
        title=core.title,
        receipt=core.receipt,
        window=window.label,
        reports=tuple(reports),
        next_moves=tuple(next_moves),
        open_questions=tuple(open_questions),
    )


def _list_investigations() -> str:
    import stormpulse.agent.integrations_manifest  # noqa: F401  (registers Integrations)
    from stormpulse.integrations import registered_integrations

    out = ["Investigations (run: stormpulse investigate <name>):"]
    for name, core in _CORE.items():
        out.append(f"  {name:<15} {core.title}")
    for integ in registered_integrations():
        for spec in integ.investigations or ():  # skylos: ignore[SKY-P403] bounded: a few integrations x their declared investigations
            qualified = f"{integ.id} {spec.name}"
            out.append(
                f"  {qualified:<15} {spec.title}"
                f"  (run: stormpulse {integ.id} investigate {spec.name})"
            )
    return "\n".join(out) + "\n"


def add_investigate_args(parser: argparse.ArgumentParser) -> None:
    """Shared flags for both doors (bare and per-integration)."""
    from stormpulse.init.files import default_config_path

    parser.add_argument(
        "name",
        nargs="?",
        default=None,
        help="investigation to run (bare = list what exists)",
    )
    parser.add_argument(
        "--since",
        default=None,
        metavar="WHEN",
        help="window start: 90m/24h/7d or 'YYYY-MM-DD [HH:MM]' (default: 24h)",
    )
    parser.add_argument(
        "--until",
        default=None,
        metavar="WHEN",
        help="window end, same syntax as --since (default: now)",
    )
    parser.add_argument(
        "--config",
        default=default_config_path(),
        help="path to config file (only investigations that need it read it)",
    )


def cmd_investigate(args: argparse.Namespace) -> None:
    """``stormpulse investigate [name]`` - list or run a core investigation."""
    if not args.name:
        sys.stdout.write(_list_investigations())
        return
    core = _CORE.get(args.name)
    if core is None:
        print(
            f"Unknown investigation {args.name!r}. Bare `stormpulse "
            "investigate` lists what exists.",
            file=sys.stderr,
        )
        sys.exit(2)
    window = parse_window(args.since, args.until, datetime.now())
    sys.stdout.write(render_case_file(core.run(args, window)))


def cmd_integration_investigate(integ_id: str, args: argparse.Namespace) -> None:  # skylos: ignore[SKY-Q301] the second door's guard ladder - CORE-005 decision 14
    """``stormpulse <integration> investigate [name]`` - the second door."""
    import stormpulse.agent.integrations_manifest  # noqa: F401  (registers Integrations)
    from stormpulse.config import ConfigError, load_config
    from stormpulse.integrations import registered_integrations

    integ = next(
        (i for i in registered_integrations() if i.id == integ_id), None,
    )
    specs = {s.name: s for s in (integ.investigations if integ else None) or ()}
    if not args.name or args.name not in specs:
        known = ", ".join(specs) or "(none declared)"
        print(f"Investigations for {integ_id}: {known}", file=sys.stderr)
        sys.exit(0 if not args.name else 2)
    try:
        config = load_config(Path(args.config))
        raw = config.integrations.get(integ_id)
        assert integ is not None  # narrowed: specs non-empty required integ
        parsed = integ.parse_config(raw) if raw is not None else None
    except ConfigError as exc:
        print(f"FATAL: config invalid: {exc}", file=sys.stderr)
        sys.exit(1)
    if parsed is None:
        print(
            f"No [{integ_id}] section in {args.config}; nothing to "
            "investigate.",
            file=sys.stderr,
        )
        sys.exit(1)
    window = parse_window(args.since, args.until, datetime.now())
    sys.stdout.write(render_case_file(specs[args.name].run(parsed, window)))
