"""Synchronous file tailer.

Reads new lines from the last-recorded byte position; rotation detected by
inode comparison. Caller runs this in a thread via ``asyncio.to_thread``.

Position is NOT advanced until the caller confirms the batch shipped - this
is the at-least-once delivery guarantee across restarts.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import select
import subprocess
import time
from datetime import UTC, datetime, timedelta

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
        ``to_position`` is after the last line actually read - unread
        lines beyond ``max_lines`` stay in the file and are picked up
        on the next call.

        Does NOT advance the stored position - call ``confirm_shipped``
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
                self._group.name,
                stored_inode,
                current_inode,
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
            self._group.name,
            str(self._group.source_path),
            to_position,
            inode,
        )
        self._pending_position = None
        self._pending_inode = None


_DOCKER_TS_PREFIX_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s"
)
_MAX_LINE_BYTES = 4096
_DOCKER_TIMEOUT_SECONDS = 30.0


def _advance_nanos(ts: str) -> str:
    """Return the next representable timestamp strictly after ``ts``.

    ``docker logs --since`` is inclusive of the boundary value, so the
    stored cursor must be **one tick past** the last line we've shipped
    or Docker will return the same line forever next cycle.

    Docker's ``--since`` flag only parses up to **microsecond** precision
    - nanosecond-precision values silently return an empty result. So we
    truncate the input to microseconds, add 1µs, and reformat. The
    smallest effective resolution is 1µs, which is plenty: we're past
    any Docker log line that shared the same microsecond window.
    """
    try:
        parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        try:
            parsed = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            # Docker's nanosecond format: truncate to 6 fractional digits.
            if "." in ts and ts.endswith("Z"):
                base, frac = ts[:-1].rsplit(".", 1)
                parsed = datetime.strptime(
                    f"{base}.{frac[:6].ljust(6, '0')}Z",
                    "%Y-%m-%dT%H:%M:%S.%fZ",
                )
            else:
                return ts
    advanced = parsed.replace(tzinfo=UTC) + timedelta(microseconds=1)
    return advanced.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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
        the last line received - stays equal to ``from_ts`` when no
        new lines arrive so we don't advance past unseen output.

        Never raises: missing binary, missing container, timeout, and
        non-zero exit codes all collapse to an empty batch.
        """
        stored = self._store.get_docker_ts(self._group.name)
        if stored is None:
            from_ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            # Persist the seed immediately so the next interval actually
            # queries a back-window. Without this, every call would re-seed
            # to "now" and nothing would ever be shipped until the first
            # confirmed batch - and the first batch can't happen if the
            # window is empty.
            self._store.set_docker_ts(
                self._group.name,
                self._group.container_name,
                from_ts,
            )
        else:
            from_ts = stored

        cmd = [
            self._group.docker_binary,
            "logs",
            "--since",
            from_ts,
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
                self._group.name,
                self._group.docker_binary,
            )
            return ([], from_ts, from_ts)
        except subprocess.TimeoutExpired:
            logger.warning("docker logs timed out for group %s", self._group.name)
            return ([], from_ts, from_ts)

        if result.returncode != 0:
            logger.warning(
                "docker logs failed for group %s (exit %d): %s",
                self._group.name,
                result.returncode,
                result.stderr.strip()[:200],
            )
            return ([], from_ts, from_ts)

        raw_lines = result.stdout.splitlines() + result.stderr.splitlines()

        capped = min(max_lines, MAX_LINES_PER_INTERVAL)
        lines = raw_lines[:capped]

        to_ts = from_ts
        for line in reversed(lines):
            m = _DOCKER_TS_PREFIX_RE.match(line)
            if m is not None:
                to_ts = _advance_nanos(m.group(1))
                break

        return (lines, from_ts, to_ts)

    def confirm_shipped(self, to_ts: str) -> None:
        """Persist ``to_ts`` as the new stored last_ts for this group."""
        self._store.set_docker_ts(
            self._group.name,
            self._group.container_name,
            to_ts,
        )


class StreamingDockerTailer:
    """Tail a container via a long-lived ``docker logs --follow`` subprocess.

    One process per container for its lifetime. Lines are drained from the
    non-blocking stdout pipe on each ``read_new_lines`` call. When the
    container stops, the subprocess exits; ``_ensure_running`` respawns
    after ``_RESPAWN_DELAY_SECONDS``.

    Position is persisted as an ISO timestamp (same contract as DockerTailer)
    so a respawn uses ``--since <last_ts>`` and doesn't replay container
    history.
    """

    _RESPAWN_DELAY_SECONDS = 5.0
    _READ_CHUNK_BYTES = 65536
    _TERMINATE_GRACE_SECONDS = 3.0
    # Floor for the per-drain read window, so a sub-second ship_interval still
    # coalesces a burst rather than returning empty-handed every cycle.
    _MIN_READ_TIMEOUT_SECONDS = 0.5

    def __init__(self, group: LogGroupConfig, position_store: LogPositionStore) -> None:
        self._group = group
        self._store = position_store
        self._proc: subprocess.Popen[bytes] | None = None
        self._buffer: bytes = b""
        # Seed so the first ever call attempts a spawn (won't be throttled).
        self._last_respawn_attempt: float = float("-inf")
        # The per-drain read window TRACKS ship_interval (slightly under it, so a
        # busy container's drain returns before the caller's next tick) instead of
        # a hardcoded 9s. The old 9s constant was sized for the old 10s default
        # and never scaled down: with ship_interval=2 it pinned the activity feed
        # at ~9-11s regardless of the setting. Now a 2s interval drains ~1.8s and
        # the feed keeps pace with the 2s metrics push.
        self._read_timeout = max(
            self._MIN_READ_TIMEOUT_SECONDS,
            self._group.ship_interval_seconds * 0.9,
        )

    def read_new_lines(self, max_lines: int) -> tuple[list[str], str, str]:
        """Drain up to ``max_lines`` lines from the running subprocess stdout.

        ``from_ts`` is the stored last_ts (seeded to "now" on first run, same
        as ``DockerTailer``). ``to_ts`` is the timestamp of the last line
        received advanced by 1µs; equals ``from_ts`` when no lines arrive.
        Never raises.
        """
        stored = self._store.get_docker_ts(self._group.name)
        if stored is None:
            from_ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            self._store.set_docker_ts(
                self._group.name,
                self._group.container_name,
                from_ts,
            )
        else:
            from_ts = stored

        if not self._ensure_running():
            return ([], from_ts, from_ts)

        raw_lines = self._drain_lines(max_lines)
        if not raw_lines:
            return ([], from_ts, from_ts)

        capped = min(max_lines, MAX_LINES_PER_INTERVAL)
        lines = raw_lines[:capped]

        to_ts = from_ts
        for line in reversed(lines):
            m = _DOCKER_TS_PREFIX_RE.match(line)
            if m is not None:
                to_ts = _advance_nanos(m.group(1))
                break

        return (lines, from_ts, to_ts)

    def confirm_shipped(self, to_ts: str) -> None:
        """Persist ``to_ts`` as the new stored last_ts for this group."""
        self._store.set_docker_ts(
            self._group.name,
            self._group.container_name,
            to_ts,
        )

    def close(self) -> None:
        """Terminate the subprocess if running. Called on agent shutdown."""
        self._terminate()

    def _ensure_running(self) -> bool:
        """Start the subprocess if not running. Returns True if running."""
        if self._is_alive():
            return True
        if self._proc is not None:
            try:
                self._proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            logger.warning(
                "docker logs process died for group %s",
                self._group.name,
            )
            self._proc = None
            self._buffer = b""
        now = time.monotonic()
        if now - self._last_respawn_attempt < self._RESPAWN_DELAY_SECONDS:
            return False
        self._last_respawn_attempt = now
        return self._spawn()

    def _spawn(self) -> bool:
        """Spawn ``docker logs --follow --timestamps --since <last_ts>``."""
        stored = self._store.get_docker_ts(self._group.name)
        from_ts = stored or datetime.now(UTC).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ",
        )
        cmd = [
            self._group.docker_binary,
            "logs",
            "--follow",
            "--timestamps",
            "--since",
            from_ts,
            self._group.container_name,
        ]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=0,
                shell=False,
            )
        except FileNotFoundError:
            logger.warning(
                "Docker binary not found for group %s: %s",
                self._group.name,
                self._group.docker_binary,
            )
            self._proc = None
            return False
        except OSError as exc:
            logger.warning(
                "Failed to spawn docker logs for group %s: %s",
                self._group.name,
                exc,
            )
            self._proc = None
            return False

        assert self._proc.stdout is not None
        fd = self._proc.stdout.fileno()
        try:
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        except OSError as exc:
            logger.warning(
                "Failed to set non-blocking on docker logs pipe for group %s: %s",
                self._group.name,
                exc,
            )
            self._terminate()
            self._proc = None
            return False
        self._buffer = b""
        logger.info(
            "Spawned docker logs --follow for group %s (since %s)",
            self._group.name,
            from_ts,
        )
        return True

    def _drain_lines(self, max_lines: int) -> list[str]:
        """Non-blocking drain from the pipe; splits on newlines."""
        if self._proc is None or self._proc.stdout is None:
            return []
        fd = self._proc.stdout.fileno()
        lines: list[str] = []
        deadline = time.monotonic() + self._read_timeout
        eof = False

        while len(lines) < max_lines:
            # Pull any complete lines out of the buffer first.
            while b"\n" in self._buffer and len(lines) < max_lines:
                line, _, rest = self._buffer.partition(b"\n")
                self._buffer = rest
                lines.append(line.decode("utf-8", errors="replace"))
            if len(lines) >= max_lines or eof:
                break

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready, _, _ = select.select([fd], [], [], remaining)
            except (OSError, ValueError):
                break
            if not ready:
                break
            try:
                chunk = os.read(fd, self._READ_CHUNK_BYTES)
            except BlockingIOError:
                continue
            except OSError:
                break
            if not chunk:
                eof = True
                continue
            self._buffer += chunk

        return lines

    def _is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _terminate(self) -> None:
        if self._proc is None:
            return
        if self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=self._TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                try:
                    self._proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass
            except OSError:
                pass
        self._proc = None
        self._buffer = b""


def _inode(path: str) -> int:
    """Public-ish helper for tests - returns the current inode of a path."""
    return os.stat(path).st_ino
