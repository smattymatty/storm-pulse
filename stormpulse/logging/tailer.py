"""Synchronous file tailer.

Reads new lines from a log file starting at the last-recorded byte
position. Detects rotation by comparing inode. Caller is responsible
for running this in a thread via ``asyncio.to_thread``.

Position is NOT advanced until the caller confirms the batch has been
shipped. This ensures at-least-once delivery across restarts.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from datetime import datetime, timezone

from stormpulse.config import LogGroupConfig
from stormpulse.logging.positions import LogPositionStore

logger = logging.getLogger(__name__)

MAX_LINES_PER_INTERVAL = 1000


class LogTailer:
    """Tail a single log file with persisted position and rotation detection."""

    def __init__(self, group: LogGroupConfig, position_store: LogPositionStore) -> None:
        self._group = group
        self._store = position_store
        # In-flight range: lines have been read but not yet confirmed shipped.
        self._pending_position: int | None = None
        self._pending_inode: int | None = None

    def read_new_lines(self, max_lines: int) -> tuple[list[str], int, int]:
        """Read up to ``max_lines`` new lines from the source file.

        Returns a tuple of (lines, from_position, to_position).
        ``from_position`` is the byte offset at the start of the read;
        ``to_position`` is after the last line actually read — unread
        lines beyond ``max_lines`` stay in the file and are picked up
        on the next call.

        Does NOT advance the stored position — call ``confirm_shipped``
        to persist ``to_position`` after the batch is acknowledged.
        """
        path = self._group.source_path
        if not path.exists():
            return ([], 0, 0)

        try:
            stat = path.stat()
        except OSError:
            return ([], 0, 0)

        stored_position, stored_inode = self._store.get(self._group.name)
        current_inode = stat.st_ino

        # Rotation detected: file replaced since last read
        if stored_inode is not None and stored_inode != current_inode:
            logger.info(
                "Log rotation detected for group %s (inode %s -> %s)",
                self._group.name, stored_inode, current_inode,
            )
            stored_position = 0

        # File shrank (truncated in place)
        if stored_position > stat.st_size:
            stored_position = 0

        start = stored_position
        lines: list[str] = []
        to_position = start

        capped = min(max_lines, MAX_LINES_PER_INTERVAL)

        try:
            with open(path, "rb") as f:
                f.seek(start)
                while len(lines) < capped:
                    raw = f.readline()
                    if not raw:
                        break
                    lines.append(raw.decode("utf-8", errors="replace"))
                    to_position = f.tell()
        except OSError as exc:
            logger.warning("Failed reading %s: %s", path, exc)
            return ([], start, start)

        self._pending_position = to_position
        self._pending_inode = current_inode
        return (lines, start, to_position)

    def confirm_shipped(self, to_position: int) -> None:
        """Persist ``to_position`` as the new stored position."""
        inode = self._pending_inode
        if inode is None:
            try:
                inode = self._group.source_path.stat().st_ino
            except OSError:
                return
        self._store.set(
            self._group.name, str(self._group.source_path), to_position, inode,
        )
        self._pending_position = None
        self._pending_inode = None


_DOCKER_TS_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s")
_MAX_LINE_BYTES = 4096
_DOCKER_TIMEOUT_SECONDS = 30.0


class DockerTailer:
    """Tail a Docker container's log output via ``docker logs --since``.

    Uses ISO timestamps as the position marker rather than byte offsets.
    Caller runs this in a thread via ``asyncio.to_thread``.
    """

    def __init__(self, group: LogGroupConfig, position_store: LogPositionStore) -> None:
        self._group = group
        self._store = position_store

    def read_new_lines(self, max_lines: int) -> tuple[list[str], str, str]:
        """Run ``docker logs --since <last_ts> --timestamps`` and return new lines.

        Returns ``(lines, from_ts, to_ts)``. ``from_ts`` is the stored
        last_ts (or the current time on first run, to avoid replaying
        the entire container history). ``to_ts`` is the timestamp of
        the last line received — stays equal to ``from_ts`` when no
        new lines arrive so we don't advance past unseen output.

        Never raises: missing binary, missing container, timeout, and
        non-zero exit codes all collapse to an empty batch.
        """
        stored = self._store.get_docker_ts(self._group.name)
        if stored is None:
            from_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        else:
            from_ts = stored

        cmd = [
            self._group.docker_binary,
            "logs",
            "--since", from_ts,
            "--timestamps",
            self._group.container_name,
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_DOCKER_TIMEOUT_SECONDS,
                shell=False,
                check=False,
            )
        except FileNotFoundError:
            logger.warning(
                "Docker binary not found for group %s: %s",
                self._group.name, self._group.docker_binary,
            )
            return ([], from_ts, from_ts)
        except subprocess.TimeoutExpired:
            logger.warning("docker logs timed out for group %s", self._group.name)
            return ([], from_ts, from_ts)

        if result.returncode != 0:
            logger.warning(
                "docker logs failed for group %s (exit %d): %s",
                self._group.name, result.returncode, result.stderr.strip()[:200],
            )
            return ([], from_ts, from_ts)

        raw_lines = result.stdout.splitlines() + result.stderr.splitlines()

        capped = min(max_lines, MAX_LINES_PER_INTERVAL)
        lines = raw_lines[:capped]

        to_ts = from_ts
        for line in reversed(lines):
            m = _DOCKER_TS_PREFIX_RE.match(line)
            if m is not None:
                to_ts = m.group(1)
                break

        return (lines, from_ts, to_ts)

    def confirm_shipped(self, to_ts: str) -> None:
        """Persist ``to_ts`` as the new stored last_ts for this group."""
        self._store.set_docker_ts(
            self._group.name, self._group.container_name, to_ts,
        )


def _inode(path: str) -> int:
    """Public-ish helper for tests — returns the current inode of a path."""
    return os.stat(path).st_ino
