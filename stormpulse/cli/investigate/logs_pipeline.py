"""The logs-pipeline investigation: per-group shipping health and parser drift."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path

from stormpulse.sdk.investigate import CaseFile, SuspectReport, Verdict, Window

from ._journal import ShippedBatch, fetch_agent_journal, run_evidence, parse_shipped


def judge_group_health(  # skylos: ignore[SKY-C304,SKY-L028] one return per verdict outcome - CORE-005 fetch/judge contract
    group_name: str,
    parser_name: str,
    batches: list[ShippedBatch],
    raw_sample: list[str] | None,
    parse: Callable[[str], object | None],
) -> SuspectReport:
    """One log group's verdict from its shipped batches plus, when it looks
    all-drop, a raw-source sample fed through its own parser.

    The 2026-07-19 lesson this encodes: ``dropped`` counts every line the
    parser returned None for, which includes deliberately suppressed noise
    (garage_s3 drops the agent's own read-only admin polls), so a steady
    lines=0 dropped=N drumbeat is not by itself a broken pipeline.
    """
    suspect = f"group {group_name}"
    if not batches:
        return SuspectReport(
            suspect=suspect,
            verdict=Verdict.CLEARED,
            evidence="No batches shipped in window (quiet source).",
        )
    shipped = sum(b.lines for b in batches)
    dropped = sum(b.dropped for b in batches)
    if shipped > 0:
        return SuspectReport(
            suspect=suspect,
            verdict=Verdict.CLEARED,
            evidence=f"{len(batches)} batches, {shipped} lines shipped, "
                     f"{dropped} dropped.",
        )
    if raw_sample is None:
        return SuspectReport(
            suspect=suspect,
            verdict=Verdict.INCONCLUSIVE,
            evidence=f"All {dropped} lines dropped, and the raw source "
                     "could not be sampled.",
            remedy="docker logs --timestamps --tail 10 <container>  "
                   "(compare against the group's parser)",
        )
    parsed = sum(1 for line in raw_sample if parse(line) is not None)
    if parsed == 0 and raw_sample:
        sample = raw_sample[-1][:120]
        return SuspectReport(
            suspect=suspect,
            verdict=Verdict.IMPLICATED,
            evidence=f"0/{len(raw_sample)} raw source lines parse under "
                     f"{parser_name!r}; sample: {sample}",
            detail="Either the source's format drifted, or every current "
                   "line is noise the parser suppresses by design. If the "
                   "sample looks like traffic you expected to ship, it is "
                   "format drift.",
        )
    return SuspectReport(
        suspect=suspect,
        verdict=Verdict.CLEARED,
        evidence=f"{parsed}/{len(raw_sample)} raw lines parse; current "
                 "drops are suppressed-by-design noise, not format drift.",
    )


def _fetch_raw_sample(group: object) -> list[str] | None:
    """Last few raw lines from a group's source (docker or file)."""
    source_type = getattr(group, "source_type", "")
    if source_type == "docker":
        out = run_evidence([
            getattr(group, "docker_binary", "/usr/bin/docker"),
            "logs", "--timestamps", "--tail", "10",
            getattr(group, "container_name", ""),
        ])
        return out.splitlines() if out else None
    try:
        text = Path(getattr(group, "source_path")).read_text(errors="replace")
    except OSError:
        return None
    return text.splitlines()[-10:] or None


def run_logs_pipeline(args: argparse.Namespace, window: Window) -> CaseFile:  # skylos: ignore[SKY-Q301] branch-per-verdict is the CORE-005 case-script contract
    from stormpulse.config import ConfigError, load_config
    from stormpulse.logging.parsers import PARSERS

    # Function-level: make_case lives in the host (__init__), which imports this
    # module for the _CORE registry.
    from . import make_case

    reports: list[SuspectReport] = []
    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        reports.append(SuspectReport(
            suspect="agent config",
            verdict=Verdict.INCONCLUSIVE,
            evidence=f"config unreadable: {exc}",
            remedy=f"stormpulse config check {args.config}",
        ))
        return make_case("logs-pipeline", window, reports, [], [])

    entries = fetch_agent_journal(window)
    batches = parse_shipped([m for _, m in entries]) if entries else []
    for group in config.log_groups:
        if not group.enabled:
            continue
        group_batches = [b for b in batches if b.group == group.name]
        all_drop = group_batches and sum(b.lines for b in group_batches) == 0
        raw_sample = _fetch_raw_sample(group) if all_drop else None
        parse = PARSERS.get(group.parser, lambda _line: None)
        reports.append(judge_group_health(
            group.name, group.parser, group_batches, raw_sample, parse,
        ))
    if not reports:
        reports.append(SuspectReport(
            suspect="log groups",
            verdict=Verdict.CLEARED,
            evidence="No enabled log groups in config.",
        ))
    return make_case("logs-pipeline", window, reports, [], [])
