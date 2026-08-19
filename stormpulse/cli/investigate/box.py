"""The box investigation: host starvation, reboots, upgrades, kernel faults."""

from __future__ import annotations

import argparse
import grp
import os
import re
from dataclasses import dataclass
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path

from stormpulse.sdk.investigate import CaseFile, SuspectReport, Verdict, Window

from ._journal import journal_ts, run_evidence


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
        out = run_evidence(["sar", "-d", "-f", str(f)])
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


def run_box(args: argparse.Namespace, window: Window) -> CaseFile:  # skylos: ignore[SKY-Q301,SKY-Q306,SKY-C304] branch-per-verdict is the CORE-005 case-script contract
    # Function-level: make_case lives in the host (__init__), which imports this
    # module for the _CORE registry.
    from . import make_case

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

    last_out = run_evidence(["last", "-F", "reboot"])
    if last_out is None:
        reports.append(SuspectReport(
            suspect="unexpected reboots",  # skylos: ignore[SKY-L027] each verdict branch names its suspect - CORE-005 case-file contract
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
            suspect="package upgrades",  # skylos: ignore[SKY-L027] each verdict branch names its suspect - CORE-005 case-file contract
            verdict=Verdict.INCONCLUSIVE,
            evidence="/var/log/apt/history.log unreadable as this user.",
            remedy="sudo tail -30 /var/log/apt/history.log",
        ))
    else:
        apt_runs = judge_apt_activity(apt_text, window)
        if apt_runs:
            stamps = ", ".join(r.strftime("%m-%d %H:%M") for r in apt_runs)  # skylos: ignore[SKY-L027] evidence timestamps share one rendering format
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
            suspect="storage latency (sar history)",  # skylos: ignore[SKY-L027] each verdict branch names its suspect - CORE-005 case-file contract
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
            suspect="kernel faults",  # skylos: ignore[SKY-L027] each verdict branch names its suspect - CORE-005 case-file contract
            verdict=Verdict.INCONCLUSIVE,
            evidence="System journal not readable as this user.",
            remedy=f'sudo journalctl -k --since "{journal_ts(window.since)}" '
                   '--no-pager | grep -iE "rcu|stall|hung|lockup|oom"',
        ))
    else:
        argv = ["journalctl", "-k", "--no-pager", "--since", journal_ts(window.since)]
        if window.until is not None:
            argv += ["--until", journal_ts(window.until)]
        kernel_out = run_evidence(argv)
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
    return make_case("box", window, reports, next_moves, open_questions)
