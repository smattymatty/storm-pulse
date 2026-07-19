"""The garage ``health`` Investigation (``stormpulse garage investigate health``).

The first Integration-declared investigation (CORE-005 investigations
surface). Read-only: it samples the container's own log and reports; it
never mutates. Checks are fetch/judge split - judges are pure over the
fetched text.

Receipt lives in the CaseFile it builds: earned 2026-07-19, when two
waves of "Worker ... exited" lines were the only in-guest witness that
garage had been shut down twice, and the absent scrub/resync lines
acquitted Garage's own maintenance for the morning's freezes.
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime

from stormpulse.garage.config import GarageConfig
from stormpulse.sdk.investigate import CaseFile, SuspectReport, Verdict, Window

_RECEIPT = (
    "earned 2026-07-19: worker-exit waves were the only witness that "
    "garage restarted twice, and quiet maintenance logs acquitted "
    "scrub/resync for the morning's freezes."
)

_WORKER_EXIT_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\S*\s.*Worker .* exited"
)
_MAINTENANCE_RE = re.compile(r"scrub|resync|snapshot|compact", re.IGNORECASE)
_SHUTDOWN_WAVE_WINDOW_SECONDS = 5.0
_SHUTDOWN_WAVE_MIN_WORKERS = 4


def judge_shutdown_waves(log_lines: list[str]) -> list[datetime]:
    """Timestamps where >= 4 workers exited within 5s: a daemon shutdown.

    Idle workers exiting en masse is Garage going down (SIGTERM path);
    a single worker exit is routine.
    """
    exits: list[datetime] = []
    for line in log_lines:
        m = _WORKER_EXIT_RE.match(line)
        if m is None:
            continue
        try:
            exits.append(datetime.strptime(m.group("ts"), "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            continue
    waves: list[datetime] = []
    i = 0
    while i < len(exits):
        j = i
        while (
            j + 1 < len(exits)
            and (exits[j + 1] - exits[i]).total_seconds()
            <= _SHUTDOWN_WAVE_WINDOW_SECONDS
        ):
            j += 1
        if j - i + 1 >= _SHUTDOWN_WAVE_MIN_WORKERS:
            waves.append(exits[i])
        i = j + 1
    return waves


def judge_maintenance(log_lines: list[str]) -> list[str]:
    """Maintenance-activity lines (scrub/resync/snapshot/compact), worker
    lifecycle chatter excluded so a shutdown doesn't read as maintenance."""
    return [
        line.strip()
        for line in log_lines
        if _MAINTENANCE_RE.search(line) and "Worker" not in line
    ]


def run_health(config: GarageConfig, window: Window) -> CaseFile:
    reports: list[SuspectReport] = []
    open_questions: list[str] = []

    log_lines = _fetch_container_log(config, window)
    if log_lines is None:
        reports.append(SuspectReport(
            suspect="container log",
            verdict=Verdict.INCONCLUSIVE,
            evidence=f"docker logs failed for {config.container_name!r}.",
            remedy=f"docker logs --timestamps --tail 20 {config.container_name}",
        ))
        return _case(window, reports, open_questions)

    waves = judge_shutdown_waves(log_lines)
    if waves:
        stamps = ", ".join(w.strftime("%m-%d %H:%M:%S") for w in waves)
        reports.append(SuspectReport(
            suspect="garage restarts",
            verdict=Verdict.IMPLICATED,
            evidence=f"daemon shutdown wave(s) at {stamps} (UTC).",
            detail="A wave of Worker-exited lines is Garage going down "
                   "gracefully - someone or something sent SIGTERM.",
        ))
        open_questions.append(
            f"Were the shutdown(s) at {stamps} operator-initiated? If not, "
            "check what restarts containers on this host."
        )
    else:
        reports.append(SuspectReport(
            suspect="garage restarts",
            verdict=Verdict.CLEARED,
            evidence="No daemon shutdown waves in window.",
        ))

    maintenance = judge_maintenance(log_lines)
    if maintenance:
        reports.append(SuspectReport(
            suspect="maintenance load (scrub/resync/snapshot)",
            verdict=Verdict.IMPLICATED,
            evidence=f"{len(maintenance)} maintenance line(s); first: "
                     f"{maintenance[0][:100]}",
            detail="Scrub and resync are periodic IO storms; correlate "
                   "their span with any freeze/flap timestamps.",
        ))
    else:
        reports.append(SuspectReport(
            suspect="maintenance load (scrub/resync/snapshot)",
            verdict=Verdict.CLEARED,
            evidence="No scrub/resync/snapshot/compact activity in window.",
        ))
    return _case(window, reports, open_questions)


def _fetch_container_log(
    config: GarageConfig, window: Window,
) -> list[str] | None:
    """The container's log lines for the window; None on any failure.

    ``docker logs --since`` wants RFC3339; the Window carries naive local
    datetimes, which match a UTC host (the Storm baseline). Timestamps
    are requested so the judges can anchor events.
    """
    argv = [
        "docker", "logs", "--timestamps",
        "--since", window.since.strftime("%Y-%m-%dT%H:%M:%S"),
    ]
    if window.until is not None:
        argv += ["--until", window.until.strftime("%Y-%m-%dT%H:%M:%S")]
    argv.append(config.container_name)
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=30.0, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    # docker logs writes container stderr to our stderr; garage logs there.
    return (result.stdout + result.stderr).splitlines()


def _case(
    window: Window,
    reports: list[SuspectReport],
    open_questions: list[str],
) -> CaseFile:
    return CaseFile(
        investigation="health",
        title="garage daemon restarts and maintenance load",
        receipt=_RECEIPT,
        window=window.label,
        reports=tuple(reports),
        open_questions=tuple(open_questions),
    )
