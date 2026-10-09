"""Boot identity: the two register facts that tell a reconnect from a reboot and
from a restart (ADR CORE-011 decision 7). Read once in ``Agent.__init__``; every
register of the process repeats the same pair."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

#: The kernel's per-boot id; a new value on every boot, the same across restarts.
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")


def boot_id(path: Path | None = None) -> str:
    """The boot id at *path* (``BOOT_ID_PATH`` when None), stripped, or ``""``
    where unreadable: never a raise."""
    try:
        return (path or BOOT_ID_PATH).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def started_at(now: datetime | None = None) -> str:
    """The process start as ISO-8601 UTC; *now* is injected only by tests."""
    return (now or datetime.now(UTC)).isoformat()
