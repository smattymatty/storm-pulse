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
import grp
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime, timedelta
from pathlib import Path

from stormpulse.init.mode import InstallMode, detect_mode
from stormpulse.sdk.investigate import CaseFile, SuspectReport, Verdict, Window

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


def _journal_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


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
# Shared fetches
# ---------------------------------------------------------------------------


def _run(argv: list[str], timeout: float = 30.0) -> str | None:
    """Run a read-only evidence command; None on any failure (the caller
    turns None into INCONCLUSIVE, never into silence)."""
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _fetch_agent_journal(window: Window) -> list[tuple[datetime, str]] | None:
    """(journald receipt time, message) pairs for the agent unit in-window."""
    argv = ["journalctl"]
    if detect_mode() is InstallMode.USER:
        argv.append("--user")
    argv += [
        "-u", "stormpulse", "--no-pager", "--output=json",
        "--since", _journal_ts(window.since),
    ]
    if window.until is not None:
        argv += ["--until", _journal_ts(window.until)]
    raw = _run(argv)
    if raw is None:
        return None
    entries: list[tuple[datetime, str]] = []
    for line in raw.splitlines():
        realtime, message = _parse_journal_json_line(line)
        if realtime is not None and message is not None:
            entries.append((realtime, message))
    return entries


def _parse_journal_json_line(line: str) -> tuple[datetime | None, str | None]:
    import json

    try:
        obj = json.loads(line)
    except ValueError:
        return (None, None)
    ts_raw = obj.get("__REALTIME_TIMESTAMP")
    message = obj.get("MESSAGE")
    if not isinstance(ts_raw, str) or not isinstance(message, str):
        return (None, None)
    try:
        realtime = datetime.fromtimestamp(int(ts_raw) / 1_000_000)
    except (ValueError, OverflowError, OSError):
        return (None, None)
    return (realtime, message)


# ---------------------------------------------------------------------------
# flaps - judges
# ---------------------------------------------------------------------------

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
        for needle, label in _DROP_FAMILIES:
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


_SHIPPED_RE = re.compile(
    r"Shipped log\.batch \S+ group=(?P<group>\S+) lines=(?P<lines>\d+) "
    r"dropped=(?P<dropped>\d+) duration_ms=(?P<ms>\d+)"
)


@dataclass(frozen=True, slots=True)
class ShippedBatch:
    group: str
    lines: int
    dropped: int
    duration_ms: int


def parse_shipped(messages: list[str]) -> list[ShippedBatch]:
    batches: list[ShippedBatch] = []
    for message in messages:
        m = _SHIPPED_RE.search(message)
        if m is not None:
            batches.append(ShippedBatch(
                group=m.group("group"),
                lines=int(m.group("lines")),
                dropped=int(m.group("dropped")),
                duration_ms=int(m.group("ms")),
            ))
    return batches


_BATCH_LINE_CAP = 200  # config ceiling for max_lines_per_batch


def run_flaps(args: argparse.Namespace, window: Window) -> CaseFile:
    reports: list[SuspectReport] = []
    next_moves: list[str] = []
    open_questions: list[str] = []

    entries = _fetch_agent_journal(window)
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
        return _case("flaps", window, reports, next_moves, open_questions)

    messages = [m for _, m in entries]
    drops = classify_drops(messages)
    drop_total = sum(v for k, v in drops.items() if k != "reconnect attempts")

    if drop_total == 0:
        reports.append(SuspectReport(
            suspect="reconnect churn",
            verdict=Verdict.CLEARED,
            evidence="0 connection drops in window; the agent held its socket.",
        ))
        return _case("flaps", window, reports, next_moves, open_questions)

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
    return _case("flaps", window, reports, next_moves, open_questions)


# ---------------------------------------------------------------------------
# box - judges
# ---------------------------------------------------------------------------


def read_proc_stat_cpu(text: str) -> tuple[int, ...] | None:
    """The aggregate cpu counters from /proc/stat content."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            fields = line.split()[1:]
            try:
                return tuple(int(f) for f in fields[:8])
            except ValueError:
                return None
    return None


def judge_cpu_pressure(
    before: tuple[int, ...], after: tuple[int, ...],
) -> dict[str, float]:
    """iowait/steal as a percent of the sampled delta (fields: user nice
    system idle iowait irq softirq steal)."""
    delta = [b - a for a, b in zip(before, after)]
    total = sum(delta)
    if total <= 0:
        return {"iowait": 0.0, "steal": 0.0}
    iowait = delta[4] if len(delta) > 4 else 0
    steal = delta[7] if len(delta) > 7 else 0
    return {
        "iowait": 100.0 * iowait / total,
        "steal": 100.0 * steal / total,
    }


_LAST_F_RE = re.compile(
    r"^reboot\s+system boot\s+\S+\s+(?P<start>\w{3} \w{3} [ \d]\d "
    r"\d{2}:\d{2}:\d{2} \d{4})"
)


def judge_reboots(last_output: str, window: Window) -> list[datetime]:
    """Reboot start times inside the window, from ``last -F reboot``."""
    reboots: list[datetime] = []
    for line in last_output.splitlines():
        m = _LAST_F_RE.match(line)
        if m is None:
            continue
        try:
            started = datetime.strptime(m.group("start"), "%a %b %d %H:%M:%S %Y")
        except ValueError:
            continue
        if started >= window.since and (
            window.until is None or started <= window.until
        ):
            reboots.append(started)
    return reboots


_APT_START_RE = re.compile(r"^Start-Date:\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})")
_UU_SCHEDULED_RE = re.compile(r"Reboot scheduled for \w+ (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def judge_apt_activity(history_text: str, window: Window) -> list[datetime]:
    """apt run start times inside the window, from /var/log/apt/history.log."""
    starts: list[datetime] = []
    for line in history_text.splitlines():
        m = _APT_START_RE.match(line)
        if m is None:
            continue
        started = datetime.strptime(f"{m.group(1)} {m.group(2)}", "%Y-%m-%d %H:%M:%S")
        if started >= window.since and (
            window.until is None or started <= window.until
        ):
            starts.append(started)
    return starts


def judge_scheduled_reboots(uu_log_text: str) -> list[datetime]:
    """Reboot times unattended-upgrades announced it scheduled."""
    scheduled: list[datetime] = []
    for m in _UU_SCHEDULED_RE.finditer(uu_log_text):
        try:
            scheduled.append(
                datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            )
        except ValueError:
            continue
    return scheduled


@dataclass(frozen=True, slots=True)
class StorageSpike:
    """One sar -d sample where a block device's await crossed the threshold."""

    at: datetime
    device: str
    await_ms: float


_STORAGE_AWAIT_THRESHOLD_MS = 100.0  # healthy virtual disks sit at 1-5ms


def judge_sar_spikes(
    text: str,
    file_date: "date_cls",
    threshold_ms: float = _STORAGE_AWAIT_THRESHOLD_MS,
) -> list[StorageSpike]:
    """Block-device latency spikes from one day's ``sar -d`` output.

    The conviction shape (2026-07-19): await inflating 100-1000x while
    tps/throughput stays at a trickle means the storage BELOW the guest
    stalled - no in-guest workload can slow several virtual disks at
    once while asking almost nothing of them. Handles both 12h (AM/PM)
    and 24h sar time formats; Average rows and loop devices excluded.
    """
    spikes: list[StorageSpike] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 8 or fields[0] == "Average:":
            continue
        if fields[1] in ("AM", "PM"):
            time_raw, fmt = f"{fields[0]} {fields[1]}", "%I:%M:%S %p"
            device, rest = fields[2], fields[3:]
        else:
            time_raw, fmt = fields[0], "%H:%M:%S"
            device, rest = fields[1], fields[2:]
        if not device[0].isalpha() or device == "DEV" or device.startswith("loop"):
            continue
        try:
            sampled = datetime.strptime(time_raw, fmt).time()
            await_ms = float(rest[-2])
        except (ValueError, IndexError):
            continue
        if await_ms >= threshold_ms:
            spikes.append(StorageSpike(
                at=datetime.combine(file_date, sampled),
                device=device,
                await_ms=await_ms,
            ))
    return spikes


def _fetch_sar_history() -> list[tuple["date_cls", str]] | None:
    """Every retained sysstat day file as (file date, ``sar -d`` text);
    None when sysstat isn't recording here."""
    day_files = sorted(Path("/var/log/sysstat").glob("sa[0-3][0-9]"))
    days: list[tuple[date_cls, str]] = []
    for f in day_files:
        out = _run(["sar", "-d", "-f", str(f)])
        if out:
            days.append((date_cls.fromtimestamp(f.stat().st_mtime), out))
    return days or None


_KERNEL_ALARM_RE = re.compile(
    r"rcu|stall|hung task|lockup|out of memory|oom-kill", re.IGNORECASE
)


def judge_kernel_lines(text: str) -> list[str]:
    """Kernel lines matching the freeze/oom alarm families, boot chatter
    excluded (RCU/clocksource init lines all appear within boot's first
    minute and carry no alarm verbs)."""
    hits: list[str] = []
    for line in text.splitlines():
        if _KERNEL_ALARM_RE.search(line) and "Preemptible hierarchical" not in line:
            hits.append(line.strip())
    return hits


def _can_read_system_journal() -> bool:
    if os.geteuid() == 0:
        return True
    allowed = {"adm", "systemd-journal"}
    try:
        names = {grp.getgrgid(g).gr_name for g in os.getgroups()}
    except KeyError:
        return False
    return bool(allowed & names)


def run_box(args: argparse.Namespace, window: Window) -> CaseFile:
    reports: list[SuspectReport] = []
    next_moves: list[str] = []
    open_questions: list[str] = []

    # CPU pressure: two /proc/stat samples one second apart. Point-in-time
    # by nature; the case file says so instead of pretending otherwise.
    import time

    try:
        before = read_proc_stat_cpu(Path("/proc/stat").read_text())
        time.sleep(1.0)
        after = read_proc_stat_cpu(Path("/proc/stat").read_text())
    except OSError:
        before = after = None
    if before is None or after is None:
        reports.append(SuspectReport(
            suspect="cpu starvation (steal/iowait)",
            verdict=Verdict.INCONCLUSIVE,
            evidence="/proc/stat unreadable.",
            remedy="vmstat 2 5  (watch the st and wa columns)",
        ))
    else:
        pressure = judge_cpu_pressure(before, after)
        starved = pressure["steal"] >= 10.0 or pressure["iowait"] >= 25.0
        reports.append(SuspectReport(
            suspect="cpu starvation (steal/iowait)",
            verdict=Verdict.IMPLICATED if starved else Verdict.CLEARED,
            evidence=f"right now: steal {pressure['steal']:.1f}%, "
                     f"iowait {pressure['iowait']:.1f}%.",
            detail="Point-in-time sample. Steal is the hypervisor giving "
                   "your CPU away; a full host-side pause shows NO steal, "
                   "only the freeze signature in `investigate flaps`.",
        ))
        if not starved:
            next_moves.append(
                "For history through an overnight window, enable sysstat "
                "(sar -u) and read it the morning after."
            )

    uu_text: str | None
    try:
        uu_text = Path(
            "/var/log/unattended-upgrades/unattended-upgrades.log"
        ).read_text()
    except OSError:
        uu_text = None

    last_out = _run(["last", "-F", "reboot"])
    if last_out is None:
        reports.append(SuspectReport(
            suspect="unexpected reboots",
            verdict=Verdict.INCONCLUSIVE,
            evidence="`last -F reboot` unavailable.",
            remedy="last -F reboot | head -5",
        ))
    else:
        reboots = judge_reboots(last_out, window)
        scheduled = judge_scheduled_reboots(uu_text) if uu_text else []
        unexplained = [
            r for r in reboots
            if not any(abs((r - s).total_seconds()) < 600 for s in scheduled)
        ]
        if not reboots:
            reports.append(SuspectReport(
                suspect="unexpected reboots",
                verdict=Verdict.CLEARED,
                evidence="No reboots in window.",
            ))
        elif not unexplained:
            reports.append(SuspectReport(
                suspect="unexpected reboots",
                verdict=Verdict.CLEARED,
                evidence=f"{len(reboots)} reboot(s) in window, all matching "
                         "an unattended-upgrades scheduled reboot.",
                detail="A boot at the scheduled reboot time is routine, not "
                       "an anomaly.",
            ))
        else:
            stamps = ", ".join(r.strftime("%m-%d %H:%M") for r in unexplained)
            reports.append(SuspectReport(
                suspect="unexpected reboots",
                verdict=Verdict.IMPLICATED,
                evidence=f"reboot(s) at {stamps} match no scheduled reboot.",
            ))
            open_questions.append(
                f"Were the reboot(s) at {stamps} operator-initiated? If not, "
                "the host restarted under you - provider-ticket territory."
            )

    apt_text: str | None
    try:
        apt_text = Path("/var/log/apt/history.log").read_text()
    except OSError:
        apt_text = None
    if apt_text is None:
        reports.append(SuspectReport(
            suspect="package upgrades",
            verdict=Verdict.INCONCLUSIVE,
            evidence="/var/log/apt/history.log unreadable as this user.",
            remedy="sudo tail -30 /var/log/apt/history.log",
        ))
    else:
        apt_runs = judge_apt_activity(apt_text, window)
        if apt_runs:
            stamps = ", ".join(r.strftime("%m-%d %H:%M") for r in apt_runs)
            reports.append(SuspectReport(
                suspect="package upgrades",
                verdict=Verdict.IMPLICATED,
                evidence=f"apt ran inside the window: {stamps}.",
                detail="dpkg on a small VPS can stall the box; correlate "
                       "these times with the flap/freeze timestamps.",
            ))
        else:
            reports.append(SuspectReport(
                suspect="package upgrades",
                verdict=Verdict.CLEARED,
                evidence="No apt activity in window.",
            ))

    history = _fetch_sar_history()
    if history is None:
        reports.append(SuspectReport(
            suspect="storage latency (sar history)",
            verdict=Verdict.INCONCLUSIVE,
            evidence="No sysstat history on this box - storage stalls in the "
                     "past are invisible without the flight recorder.",
            detail="sar samples CPU and per-disk latency every 10 minutes "
                   "around the clock; it is how a host-side storage stall "
                   "gets caught after the fact.",
            remedy="sudo apt install sysstat && sudo systemctl enable --now "
                   "sysstat sysstat-collect.timer",
        ))
    else:
        all_spikes = [
            s for day, text in history for s in judge_sar_spikes(text, day)
        ]
        in_window = [
            s for s in all_spikes
            if s.at >= window.since
            and (window.until is None or s.at <= window.until)
        ]
        days_hit = len({s.at.date() for s in all_spikes})
        if in_window:
            worst = max(in_window, key=lambda s: s.await_ms)
            reports.append(SuspectReport(
                suspect="storage latency (sar history)",
                verdict=Verdict.IMPLICATED,
                evidence=f"{len(in_window)} sample(s) >= "
                         f"{int(_STORAGE_AWAIT_THRESHOLD_MS)}ms await in "
                         f"window; worst {worst.await_ms:.0f}ms on "
                         f"{worst.device} at {worst.at:%m-%d %H:%M}.",
                detail="High await at trickle load is the storage below the "
                       "guest stalling, not guest workload - "
                       "provider-ticket territory.",
            ))
        elif all_spikes:
            worst = max(all_spikes, key=lambda s: s.await_ms)
            reports.append(SuspectReport(
                suspect="storage latency (sar history)",
                verdict=Verdict.CLEARED,
                evidence=f"No spikes in this window, but {len(all_spikes)} "
                         f"sample(s) >= {int(_STORAGE_AWAIT_THRESHOLD_MS)}ms "
                         f"await across {days_hit} recorded day(s); worst "
                         f"{worst.await_ms:.0f}ms on {worst.device} at "
                         f"{worst.at:%m-%d %H:%M}.",
                detail="Chronic background storage latency: cleared for this "
                       "window, ticket material overall (sar -d has the rows).",
            ))
        else:
            reports.append(SuspectReport(
                suspect="storage latency (sar history)",
                verdict=Verdict.CLEARED,
                evidence="No block-device awaits >= "
                         f"{int(_STORAGE_AWAIT_THRESHOLD_MS)}ms anywhere in "
                         "the sysstat retention window.",
            ))

    if not _can_read_system_journal():
        reports.append(SuspectReport(
            suspect="kernel faults",
            verdict=Verdict.INCONCLUSIVE,
            evidence="System journal not readable as this user.",
            remedy=f'sudo journalctl -k --since "{_journal_ts(window.since)}" '
                   '--no-pager | grep -iE "rcu|stall|hung|lockup|oom"',
        ))
    else:
        argv = ["journalctl", "-k", "--no-pager", "--since", _journal_ts(window.since)]
        if window.until is not None:
            argv += ["--until", _journal_ts(window.until)]
        kernel_out = _run(argv)
        if kernel_out is None:
            reports.append(SuspectReport(
                suspect="kernel faults",
                verdict=Verdict.INCONCLUSIVE,
                evidence="journalctl -k failed.",
                remedy="sudo journalctl -k --no-pager | tail -50",
            ))
        else:
            hits = judge_kernel_lines(kernel_out)
            if hits:
                reports.append(SuspectReport(
                    suspect="kernel faults",
                    verdict=Verdict.IMPLICATED,
                    evidence=f"{len(hits)} alarm line(s); first: {hits[0][:100]}",
                ))
            else:
                reports.append(SuspectReport(
                    suspect="kernel faults",
                    verdict=Verdict.CLEARED,
                    evidence="No rcu/stall/hung/lockup/oom lines in window.",
                    detail="A clean kernel log does NOT acquit the "
                           "hypervisor: full pauses leave no in-guest trace.",
                ))
    return _case("box", window, reports, next_moves, open_questions)


# ---------------------------------------------------------------------------
# logs-pipeline
# ---------------------------------------------------------------------------


def judge_group_health(
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
        out = _run([
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


def run_logs_pipeline(args: argparse.Namespace, window: Window) -> CaseFile:
    from stormpulse.config import ConfigError, load_config
    from stormpulse.logging.parsers import PARSERS

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
        return _case("logs-pipeline", window, reports, [], [])

    entries = _fetch_agent_journal(window)
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
    return _case("logs-pipeline", window, reports, [], [])


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
        for spec in integ.investigations or ():
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


def cmd_integration_investigate(integ_id: str, args: argparse.Namespace) -> None:
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
