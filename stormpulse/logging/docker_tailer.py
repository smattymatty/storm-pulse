"""Docker tailers: ``docker logs --since`` per interval, or one ``--follow`` stream.

Both kinds checkpoint an ISO timestamp, not a byte offset.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import select
import subprocess
import threading
import time
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from stormpulse.config import LogGroupConfig
from stormpulse.logging.positions import LogPositionStore
from stormpulse.logging.receipt import Receipt
from stormpulse.logging.tailer import MAX_LINES_PER_INTERVAL, consume_filtered, is_own

logger = logging.getLogger(__name__)

# Docker --timestamps to the microsecond; --since parses no finer.
_DOCKER_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

_DOCKER_TS_PREFIX_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s"
)
_DOCKER_TIMEOUT_SECONDS = 30.0


def _advance_nanos(ts: str) -> str:
    """Return the next representable timestamp strictly after ``ts``.

    ``--since`` is inclusive, so the cursor sits one tick past the last line
    shipped, or Docker returns that line forever. ``--since`` parses no finer
    than microseconds (nanoseconds silently return nothing), so the tick is 1µs.
    """
    parsed = _parse_docker_ts(ts)
    if parsed is None:
        return ts
    advanced = parsed + timedelta(microseconds=1)
    return advanced.strftime(_DOCKER_TS_FORMAT)


def _parse_docker_ts(ts: str) -> datetime | None:
    """A Docker ``...Z`` timestamp as a UTC instant, truncated to microseconds."""
    for fmt in (_DOCKER_TS_FORMAT, "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(ts, fmt).replace(tzinfo=UTC)
        except ValueError:
            pass
    # Docker's nanosecond format: truncate to 6 fractional digits.
    if "." in ts and ts.endswith("Z"):
        base, frac = ts[:-1].rsplit(".", 1)
        try:
            return datetime.strptime(
                f"{base}.{frac[:6].ljust(6, '0')}Z", _DOCKER_TS_FORMAT
            ).replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def _confirm_docker_ts(
    group: LogGroupConfig, store: LogPositionStore, receipt: Receipt
) -> bool:
    """Both Docker kinds: store the end ts of a receipt this container issued."""
    start, end = receipt.from_position, receipt.to_position
    if not is_own(receipt, group, group.container_name):
        return False
    if not isinstance(start, str) or not isinstance(end, str):
        return False
    stored = store.get_docker_ts(group.name)
    if stored is not None and not _covers(start, stored, end):
        return False
    store.set_docker_ts(group.name, group.container_name, end)
    return True


def _docker_from_ts(group: LogGroupConfig, store: LogPositionStore) -> str:
    """Both Docker kinds: the stored ts, or "now" persisted as the seed.

    Persisting the seed makes the next interval query a back-window; re-seeding
    to "now" every call would ship nothing until a first confirm that never comes.
    """
    stored = store.get_docker_ts(group.name)
    if stored is not None:
        return stored
    seed = datetime.now(UTC).strftime(_DOCKER_TS_FORMAT)
    store.set_docker_ts(group.name, group.container_name, seed)
    return seed


def _last_ts(lines: list[str], from_ts: str) -> str:
    """One tick past the last timestamped line, or ``from_ts`` if none carries one."""
    for line in reversed(lines):
        m = _DOCKER_TS_PREFIX_RE.match(line)
        if m is not None:
            return _advance_nanos(m.group(1))
    return from_ts


def _docker_batch(
    group: LogGroupConfig, from_ts: str, raw_lines: list[str], max_lines: int
) -> tuple[list[str], Receipt]:
    """Both Docker kinds: the capped lines and a receipt from ``from_ts`` past the last."""
    lines = raw_lines[: min(max_lines, MAX_LINES_PER_INTERVAL)]
    to_ts = _last_ts(lines, from_ts)
    return (lines, Receipt(group.name, group.container_name, None, from_ts, to_ts))


def _covers(start: str, stored: str, end: str) -> bool:
    """True when ``start <= stored < end`` as instants: no rewind, no skipped gap."""
    a, s, e = (_parse_docker_ts(ts) for ts in (start, stored, end))
    if s is None:
        return True  # an unreadable checkpoint protects nothing
    return a is not None and e is not None and a <= s < e


class DockerTailer:
    """Tail a Docker container's log output via ``docker logs --since``.

    Uses ISO timestamps as the position marker rather than byte offsets.
    Caller runs this in a thread via ``asyncio.to_thread``.
    """

    def __init__(self, group: LogGroupConfig, position_store: LogPositionStore) -> None:
        self._group = group
        self._store = position_store

    def read_new_lines(
        self,
        max_lines: int,
        # Cursor-contract signature: a pull read never gates on pending.
        pending: Collection[Receipt] | None = None,  # skylos: ignore
    ) -> tuple[list[str], Receipt]:
        """Run ``docker logs --since <last_ts> --timestamps``; return lines and receipt.

        The receipt runs from the stored ts (seeded to "now" on first run, so no
        history replays) to one tick past the last line, or stays at ``from_ts``.
        Never raises: every failure to run collapses to an empty batch.
        """
        from_ts = _docker_from_ts(self._group, self._store)
        return _docker_batch(self._group, from_ts, self._run_logs(from_ts), max_lines)

    def _run_logs(self, from_ts: str) -> list[str]:
        """stdout then stderr lines of ``docker logs --since``; [] when it could not run."""
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
            return []
        except subprocess.TimeoutExpired:
            logger.warning("docker logs timed out for group %s", self._group.name)
            return []

        if result.returncode != 0:
            logger.warning(
                "docker logs failed for group %s (exit %d): %s",
                self._group.name,
                result.returncode,
                result.stderr.strip()[:200],
            )
            return []
        return result.stdout.splitlines() + result.stderr.splitlines()

    def confirm_shipped(self, receipt: Receipt) -> bool:
        """Store the receipt's end ts; refuse one this group's container never issued."""
        return _confirm_docker_ts(self._group, self._store, receipt)

    def skip_filtered(
        self, receipt: Receipt, pending: Collection[Receipt] | None
    ) -> bool:
        """Consume a wholly filtered read; see ``consume_filtered``."""
        return consume_filtered(self.confirm_shipped, receipt, pending)


@dataclass(slots=True)
class _Range:
    """An issued range, done once ACKed or wholly filtered."""

    receipt: Receipt
    done: bool = False


class StreamingDockerTailer:
    """Tail a container via a long-lived ``docker logs --follow`` subprocess.

    One process per container, drained from its non-blocking stdout pipe on
    each ``read_new_lines``; a dead one respawns ``--since`` the checkpoint (an
    ISO ts, as DockerTailer). Receipts chain from the stream's last end; the
    checkpoint moves only to the end of the confirmed prefix, and a range lost
    from ``pending`` rewinds the follow.
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
        # Where the stream is (None: at the checkpoint), and every range issued since.
        self._stream_ts: str | None = None
        self._issued: list[_Range] = []
        # Confirm (ACK worker) and read (log worker) run in different threads;
        # held for bookkeeping only, never across the pipe wait.
        self._lock = threading.Lock()
        # Seed so the first ever call attempts a spawn (won't be throttled).
        self._last_respawn_attempt: float = float("-inf")
        # The drain window tracks ship_interval, just under it, so a busy drain
        # returns before the next tick (a fixed 9s pinned a 2s feed at ~9-11s).
        self._read_timeout = max(
            self._MIN_READ_TIMEOUT_SECONDS,
            self._group.ship_interval_seconds * 0.9,
        )

    def read_new_lines(
        self, max_lines: int, pending: Collection[Receipt] | None = None
    ) -> tuple[list[str], Receipt]:
        """Drain up to ``max_lines`` lines from the running subprocess stdout.

        ``from_ts``: the previous receipt's end, else the stored ts (seeded to
        "now", as ``DockerTailer``). ``to_ts``: last line's ts plus 1µs, else
        ``from_ts``. Lines issue a range; an open range absent from ``pending``
        rewinds first (None never). Never raises.
        """
        with self._lock:
            if pending is not None and self._lost(pending):
                self._rewind()
            running = self._ensure_running()  # a respawn resets _stream_ts first
            from_ts = self._stream_ts or _docker_from_ts(self._group, self._store)
        # Only this worker spawns or drains, so nothing respawns while unlocked.
        raw_lines = self._drain_lines(max_lines) if running else []
        with self._lock:
            lines, receipt = _docker_batch(self._group, from_ts, raw_lines, max_lines)
            if lines:
                self._stream_ts = str(receipt.to_position)
                self._issued.append(_Range(receipt))
            return lines, receipt

    def confirm_shipped(self, receipt: Receipt) -> bool:
        """Mark an issued range done; store the end of the done prefix, never past it.

        Refused (False): a receipt not among the open ranges issued (another
        group, a duplicate, or one from before this tailer or its last spawn).
        Matched by value; harmless collisions: a zero-width range never moves the
        checkpoint, a late pre-rewind ACK equal to a new range confirms delivered lines.
        """
        with self._lock:
            for issued in self._issued:
                if issued.receipt == receipt and not issued.done:
                    issued.done = True
                    break
            else:
                return False
            end: str | None = None
            while self._issued and self._issued[0].done:
                end = str(self._issued.pop(0).receipt.to_position)
            if end is not None:
                self._store.set_docker_ts(
                    self._group.name, self._group.container_name, end
                )
            return True

    def skip_filtered(
        self,
        receipt: Receipt,
        # Cursor-contract signature: a filtered range is done at read.
        pending: Collection[Receipt] | None,  # skylos: ignore
    ) -> bool:
        """A wholly filtered range never goes on the wire: done at read, even unknown ``pending``."""
        return self.confirm_shipped(receipt)

    def close(self) -> None:
        """Terminate the subprocess if running. Called on agent shutdown."""
        with self._lock:
            self._terminate()

    def _lost(self, pending: Collection[Receipt]) -> bool:
        """True when a range issued, never confirmed, is no longer awaiting its ACK."""
        live = set(pending)
        return any(not r.done and r.receipt not in live for r in self._issued)

    def _rewind(self) -> None:
        """End the follow and lift the throttle: ``_ensure_running`` respawns now."""
        logger.warning(
            "Rewinding docker logs for group %s to %s: a batch was lost "
            "(re-reading %d ranges, %d already confirmed)",
            self._group.name,
            self._store.get_docker_ts(self._group.name),
            len(self._issued),
            sum(r.done for r in self._issued),
        )
        self._terminate()
        self._last_respawn_attempt = float("-inf")

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
        """Spawn ``docker logs --follow --timestamps --since <last_ts>``.

        Every spawn is a rewind point: the stream restarts at the checkpoint, so
        the ranges issued before it are dropped and their late ACKs refused.
        """
        self._stream_ts = None
        self._issued = []
        from_ts = _docker_from_ts(self._group, self._store)
        cmd = [
            self._group.docker_binary,
            "logs",
            "--follow",
            "--timestamps",
            "--since",
            from_ts,
            self._group.container_name,
        ]
        if not self._open(cmd):
            return False
        logger.info(
            "Spawned docker logs --follow for group %s (since %s)",
            self._group.name,
            from_ts,
        )
        return True

    def _open(self, cmd: list[str]) -> bool:
        """Start ``cmd`` on a non-blocking stdout pipe; False, logged, on failure."""
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
        return True

    def _drain_lines(self, max_lines: int) -> list[str]:
        """Non-blocking drain from the pipe; splits on newlines."""
        if self._proc is None or self._proc.stdout is None:
            return []
        fd = self._proc.stdout.fileno()
        lines: list[str] = []
        deadline = time.monotonic() + self._read_timeout
        while True:
            self._take_lines(lines, max_lines)
            if len(lines) >= max_lines:
                return lines
            chunk = self._read_chunk(fd, deadline - time.monotonic())
            if chunk is None:
                return lines
            self._buffer += chunk

    def _take_lines(self, lines: list[str], max_lines: int) -> None:
        """Move complete lines out of the buffer into ``lines``, up to ``max_lines``."""
        while b"\n" in self._buffer and len(lines) < max_lines:
            line, _, self._buffer = self._buffer.partition(b"\n")
            lines.append(line.decode("utf-8", errors="replace"))

    def _read_chunk(self, fd: int, remaining: float) -> bytes | None:
        """One wait and read within ``remaining``: bytes, maybe empty, or None to stop.

        None on timeout, error or EOF; empty when the read would block.
        """
        try:
            ready = remaining > 0 and select.select([fd], [], [], remaining)[0]
        except (OSError, ValueError):
            ready = []
        if not ready:
            return None
        cap = self._READ_CHUNK_BYTES
        try:
            chunk = os.read(fd, cap)  # skylos: ignore[SKY-P401] one chunk, capped
        except BlockingIOError:
            return b""
        except OSError:
            return None
        return chunk or None

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
