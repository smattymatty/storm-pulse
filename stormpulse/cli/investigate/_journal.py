"""Shared evidence machinery: journal fetches and shipped-batch parsing."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime

from stormpulse.init.mode import InstallMode, detect_mode
from stormpulse.sdk.investigate import Window


def journal_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def run_evidence(argv: list[str], timeout: float = 30.0) -> str | None:
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


def fetch_agent_journal(window: Window) -> list[tuple[datetime, str]] | None:
    """(journald receipt time, message) pairs for the agent unit in-window."""
    argv = ["journalctl"]
    if detect_mode() is InstallMode.USER:
        argv.append("--user")
    argv += [
        "-u", "stormpulse", "--no-pager", "--output=json",
        "--since", journal_ts(window.since),
    ]
    if window.until is not None:
        argv += ["--until", journal_ts(window.until)]
    raw = run_evidence(argv)
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
