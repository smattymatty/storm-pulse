"""The flaps investigation: agent websocket reconnect churn."""

from __future__ import annotations

import argparse
import re
from datetime import datetime

from stormpulse.sdk.investigate import CaseFile, SuspectReport, Verdict, Window

from ._journal import fetch_agent_journal, parse_shipped

_APP_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\s")

# Order matters: keepalive lines also contain "no close frame received".
_DROP_FAMILIES: tuple[tuple[str, str], ...] = (
    ("keepalive ping timeout", "keepalive timeout (pings unanswered 20s)"),
    ("timed out during handshake", "handshake timeout (peer couldn't accept in 10s)"),
    ("no close frame received or sent", "abrupt TCP drop (a process died or restarted)"),
)

FREEZE_THRESHOLD_SECONDS = 15.0


def judge_freezes(
    entries: list[tuple[datetime, str]],
    threshold: float = FREEZE_THRESHOLD_SECONDS,
) -> list[tuple[str, float]]:
    """(app timestamp, lag seconds) for every line whose journald receipt
    lagged its own formatted timestamp by >= threshold.

    Python logging stamps at emit and writes immediately; journald stamps
    on receipt. Normally microseconds apart. A large gap means the process
    (or the whole box) did not get scheduled between formatting and
    delivery - the freeze signature that cracked 2026-07-19, visible even
    when nothing in-guest logs an error.
    """
    freezes: list[tuple[str, float]] = []
    for realtime, message in entries:
        m = _APP_TS_RE.match(message)
        if m is None:
            continue
        try:
            app_ts = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
        lag = (realtime - app_ts).total_seconds()
        if lag >= threshold:
            freezes.append((m.group(1), lag))
    return freezes


def classify_drops(messages: list[str]) -> dict[str, int]:
    """Tally connection drops by error family, plus reconnect attempts."""
    counts = {label: 0 for _, label in _DROP_FAMILIES}
    counts["reconnect attempts"] = 0
    for message in messages:
        if "Reconnecting in" in message:
            counts["reconnect attempts"] += 1
            continue
        if "Connection closed" not in message and "Connection error" not in message:
            continue
        for needle, label in _DROP_FAMILIES:  # skylos: ignore[SKY-P403] inner scan over 3 constant drop families
            if needle in message:
                counts[label] += 1
                break
    return counts


def count_command_results(messages: list[str]) -> tuple[int, int]:
    """(all command results, garage_refresh results) sent in-window."""
    total = sum(1 for m in messages if "Sent result for" in m)
    refresh = sum(
        1 for m in messages if "Sent result for" in m and "garage_refresh" in m
    )
    return (total, refresh)


_BATCH_LINE_CAP = 200  # config ceiling for max_lines_per_batch


def run_flaps(args: argparse.Namespace, window: Window) -> CaseFile:  # skylos: ignore[SKY-Q301,SKY-C304] branch-per-verdict is the CORE-005 case-script contract
    # Function-level: make_case lives in the host (__init__), which imports this
    # module for the _CORE registry.
    from . import make_case

    reports: list[SuspectReport] = []
    next_moves: list[str] = []
    open_questions: list[str] = []

    entries = fetch_agent_journal(window)
    if not entries:
        # None (journalctl failed) and [] (zero entries) both mean we saw
        # nothing - and an unwitnessed window must never read as CLEARED
        # (journalctl exits 0 with no output for a unit that does not
        # exist here; the 2026-07-19 `journalctl -k` trap, same shape).
        reports.append(SuspectReport(
            suspect="agent journal",
            verdict=Verdict.INCONCLUSIVE,
            evidence="No stormpulse journal entries in window - agent not "
                     "installed here, not running, or window predates the journal.",
            remedy="stormpulse logs --no-follow  (does the unit log at all?)",
        ))
        return make_case("flaps", window, reports, next_moves, open_questions)

    messages = [m for _, m in entries]
    drops = classify_drops(messages)
    drop_total = sum(v for k, v in drops.items() if k != "reconnect attempts")

    if drop_total == 0:
        reports.append(SuspectReport(
            suspect="reconnect churn",
            verdict=Verdict.CLEARED,
            evidence="0 connection drops in window; the agent held its socket.",
        ))
        return make_case("flaps", window, reports, next_moves, open_questions)

    taxonomy = ", ".join(f"{v} x {k}" for k, v in drops.items() if v)
    reports.append(SuspectReport(
        suspect="reconnect churn",
        verdict=Verdict.IMPLICATED,
        evidence=f"{drop_total} drops: {taxonomy}.",
        detail="A healthy agent reconnects only on a deploy or restart.",
    ))

    total_cmds, refresh_cmds = count_command_results(messages)
    if refresh_cmds == 0:
        reports.append(SuspectReport(
            suspect="refresh storm",
            verdict=Verdict.CLEARED,
            evidence=f"0 garage_refresh results in window ({total_cmds} commands total).",
            detail="The one unbounded Garage admin path saw no traffic.",
        ))
    else:
        reports.append(SuspectReport(
            suspect="refresh storm",
            verdict=Verdict.IMPLICATED if refresh_cmds > 30 else Verdict.CLEARED,
            evidence=f"{refresh_cmds} garage_refresh results in window.",
            detail="garage_refresh has no debounce; a client loop here hits "
                   "Garage's admin API unbounded.",
        ))

    batches = parse_shipped(messages)
    capped = sum(1 for b in batches if b.lines >= _BATCH_LINE_CAP)
    peak = max((b.lines for b in batches), default=0)
    # Proportional, not absolute: a 24h window ships thousands of batches,
    # so a handful at the cap is burst absorption working, not overload
    # (first live run: 6 capped of 9561 read as IMPLICATED - wrong).
    overloaded = capped > max(10, len(batches) // 100)
    reports.append(SuspectReport(
        suspect="log shipping overload",
        verdict=Verdict.IMPLICATED if overloaded else Verdict.CLEARED,
        evidence=f"{len(batches)} batches shipped; peak {peak} lines; "
                 f"{capped} at the {_BATCH_LINE_CAP}-line cap.",
        detail="A steady duration_ms near ship_interval x 0.9 is the drain "
               "window working as designed, not a stall.",
    ))

    freezes = judge_freezes(entries)
    if freezes:
        worst = max(freezes, key=lambda f: f[1])
        reports.append(SuspectReport(
            suspect="process/box freeze",
            verdict=Verdict.IMPLICATED,
            evidence=f"{len(freezes)} log lines reached journald >= "
                     f"{int(FREEZE_THRESHOLD_SECONDS)}s late; worst {worst[1]:.0f}s "
                     f"at {worst[0]}.",
            detail="The process (or the whole box) was not scheduled between "
                   "formatting a line and delivering it. A frozen guest shows "
                   "this even when nothing in-guest errors.",
        ))
        next_moves.append(
            "Run `stormpulse investigate box` over the same window: steal, "
            "iowait, upgrades, kernel faults, reboots, and the sar "
            "storage-latency tape (it names the install command if the box "
            "has no sysstat history yet)."
        )
    else:
        reports.append(SuspectReport(
            suspect="process/box freeze",
            verdict=Verdict.CLEARED,
            evidence="No journald receipt lag >= "
                     f"{int(FREEZE_THRESHOLD_SECONDS)}s on any line in window.",
            detail="Drops without local freeze signature point at the "
                   "control plane or the network path, not this box.",
        ))
        open_questions.append(
            "Did the control plane deploy or restart at the drop times? "
            "A fleet-wide same-minute cluster is the backend-bounce signature."
        )
    return make_case("flaps", window, reports, next_moves, open_questions)  # skylos: ignore[SKY-L027] each early return files the same case name
