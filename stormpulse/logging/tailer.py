"""Synchronous file tailer.

Reads new lines from the last-recorded byte position; rotation detected by
inode comparison. Caller runs this in a thread via ``asyncio.to_thread``.

Position is NOT advanced until the caller confirms the batch shipped - this
is the at-least-once delivery guarantee across restarts.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import select
import subprocess
import time
from datetime import UTC, datetime, timedelta

from stormpulse.config import LogGroupConfig
from stormpulse.logging.positions import LogPositionStore
from stormpulse.logging.receipt import FileGeneration, Receipt

logger = logging.getLogger(__name__)

MAX_LINES_PER_INTERVAL = 1000

_JOURNALCTL_BINARY = "journalctl"
_JOURNALCTL_TIMEOUT_SECONDS = 15
# Bytes of a file's head that name its generation alongside the inode.
_HEAD_BYTES = 4096
# Docker --timestamps to the microsecond; --since parses no finer.
_DOCKER_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def _is_own(receipt: Receipt, group: LogGroupConfig, source: str) -> bool:
    """True when ``receipt`` was issued for this group's current source."""
    return receipt.group == group.name and receipt.source == source


def _generation(fd: int, inode: int, upto: int) -> FileGeneration:
    """The generation of open file ``fd``: its inode and its first ``upto`` bytes (capped)."""
    head = os.pread(fd, min(_HEAD_BYTES, upto), 0)
    digest = hashlib.blake2b(head, digest_size=16).digest()
    return FileGeneration(inode, len(head), digest)


class LogTailer:
    """Tail a single log file with persisted position and rotation detection."""

    def __init__(self, group: LogGroupConfig, position_store: LogPositionStore) -> None:
        self._group = group
        self._store = position_store

    def _receipt(
        self, generation: FileGeneration | None, start: int, end: int
    ) -> Receipt:
        return Receipt(
            self._group.name, str(self._group.source_path), generation, start, end
        )

    def read_new_lines(self, max_lines: int) -> tuple[list[str], Receipt]:
        """Read up to ``max_lines`` new lines; return them and the read's receipt.

        The receipt's range runs from the start offset to just past the last
        line read; its generation names the file read. Nothing is stored until
        ``confirm_shipped`` is handed that receipt after the batch is ACKed.
        """
        path = self._group.source_path
        try:
            stat = path.stat()
        except OSError:  # missing, or unreadable
            return ([], self._receipt(None, 0, 0))
        start = self._start(stat)
        capped = min(max_lines, MAX_LINES_PER_INTERVAL)
        lines: list[str] = []
        try:
            with open(path, "rb") as f:
                f.seek(start)
                while len(lines) < capped and (raw := f.readline()):
                    lines.append(raw.decode("utf-8", errors="replace"))
                end = f.tell()
                generation = _generation(f.fileno(), stat.st_ino, end)
        except OSError as exc:
            logger.warning("Failed reading %s: %s", path, exc)
            return ([], self._receipt(None, start, start))
        return (lines, self._receipt(generation, start, end))

    def _start(self, stat: os.stat_result) -> int:
        """The stored offset, or 0 when the inode changed or the file shrank below it."""
        stored_position, stored_inode = self._store.get(self._group.name)
        if stored_inode is not None and stored_inode != stat.st_ino:
            logger.info(
                "Log rotation detected for group %s (inode %s -> %s)",
                self._group.name,
                stored_inode,
                stat.st_ino,
            )
            return 0
        return 0 if stored_position > stat.st_size else stored_position

    def confirm_shipped(self, receipt: Receipt) -> bool:
        """Store the receipt's end if the file still holds the generation it read.

        Refused (False): another group or source, an inode the path no longer
        names, a shorter file, a rewritten head, or a range that does not cover
        the checkpoint (a late or duplicate ACK ends at or before it; one past
        it would skip an unconfirmed gap).
        """
        owned = self._own_range(receipt)
        if owned is None:
            return False
        gen, start, end = owned
        size = self._still_holds(gen, end)
        if size is None:
            return False
        stored, stored_inode = self._store.get(self._group.name)
        # A checkpoint from another inode or past the end is an earlier generation's.
        base = stored if stored_inode == gen.inode and stored <= size else 0
        if not start <= base < end:
            return False
        self._store.set(self._group.name, str(self._group.source_path), end, gen.inode)
        return True

    def _own_range(self, receipt: Receipt) -> tuple[FileGeneration, int, int] | None:
        """The receipt's generation and byte range, or None if this tailer never issued it."""
        gen, start, end = receipt.generation, receipt.from_position, receipt.to_position
        if not _is_own(receipt, self._group, str(self._group.source_path)):
            return None
        if gen is None or not isinstance(start, int) or not isinstance(end, int):
            return None
        return gen, start, end

    def _still_holds(self, gen: FileGeneration, end: int) -> int | None:
        """The file's size if the path still names ``gen`` and reaches ``end``, else None."""
        try:
            with open(self._group.source_path, "rb") as f:
                st = os.fstat(f.fileno())
                same = _generation(f.fileno(), st.st_ino, gen.head_len) == gen
        except OSError:
            return None
        return st.st_size if same and st.st_size >= end else None


_DOCKER_TS_PREFIX_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s"
)
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
    if not _is_own(receipt, group, group.container_name):
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

    def _receipt(self, from_ts: str, to_ts: str) -> Receipt:
        return Receipt(
            self._group.name, self._group.container_name, None, from_ts, to_ts
        )

    def read_new_lines(self, max_lines: int) -> tuple[list[str], Receipt]:
        """Run ``docker logs --since <last_ts> --timestamps``; return lines and receipt.

        The receipt runs from the stored ts (seeded to "now" on first run, so no
        history replays) to one tick past the last line, or stays at ``from_ts``.
        Never raises: every failure to run collapses to an empty batch.
        """
        from_ts = _docker_from_ts(self._group, self._store)
        raw_lines = self._run_logs(from_ts)
        lines = raw_lines[: min(max_lines, MAX_LINES_PER_INTERVAL)]
        return (lines, self._receipt(from_ts, _last_ts(lines, from_ts)))

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


class JournaldTailer:
    """Tail a systemd unit's journal via ``journalctl --after-cursor``.

    The position marker is the journal's own cursor, an opaque token the
    journal hands back with every record, so there is no byte offset to keep
    in sync and no rotation to detect: the journal owns both. Caller runs this
    in a thread via ``asyncio.to_thread``.

    Why a unit rather than a file: a systemd-supervised service writes to the
    journal by default. Pointing the agent at the journal means the service
    needs no ``StandardOutput=`` redirect, no log directory, no permissions on
    it, and no rotation policy, and the journal supplies the timestamp that a
    bare ``eprintln``/``print`` line does not carry.

    Never raises: a missing ``journalctl``, an unknown unit, a timeout and a
    non-zero exit all collapse to an empty batch, matching ``DockerTailer``.
    """

    def __init__(self, group: LogGroupConfig, position_store: LogPositionStore) -> None:
        self._group = group
        self._store = position_store

    def _run(self, args: list[str]) -> str | None:
        """Run journalctl, returning stdout, or None when it could not run."""
        try:
            result = subprocess.run(
                [
                    _JOURNALCTL_BINARY,
                    "--unit",
                    self._group.unit,
                    "--output",
                    "json",
                    "--no-pager",
                    *args,
                ],
                capture_output=True,
                text=True,
                timeout=_JOURNALCTL_TIMEOUT_SECONDS,
                shell=False,
                check=False,
            )
        except FileNotFoundError:
            logger.warning(
                "journalctl not found for group %s; journald groups need systemd",
                self._group.name,
            )
            return None
        except subprocess.TimeoutExpired:
            logger.warning("journalctl timed out for group %s", self._group.name)
            return None

        if result.returncode != 0:
            logger.warning(
                "journalctl failed for group %s (exit %d): %s",
                self._group.name,
                result.returncode,
                result.stderr.strip()[:200],
            )
            return None
        return result.stdout

    @staticmethod
    def _cursor_of(record_line: str) -> str:
        """The ``__CURSOR`` of one JSON record, or '' if it has none."""
        try:
            record = json.loads(record_line)
        except (ValueError, TypeError):
            return ""
        if not isinstance(record, dict):
            return ""
        cursor = record.get("__CURSOR")
        return cursor if isinstance(cursor, str) else ""

    def _receipt(self, from_cursor: str, to_cursor: str) -> Receipt:
        return Receipt(self._group.name, self._group.unit, None, from_cursor, to_cursor)

    def _seed(self) -> None:
        """Store the newest record's cursor; an empty journal stays unseeded, retried next interval."""
        seed = self._run(["--lines", "1"])
        cursor = self._cursor_of(seed.strip().splitlines()[-1]) if seed else ""
        if cursor:
            self._store.set_cursor(self._group.name, self._group.unit, cursor)

    def read_new_lines(self, max_lines: int) -> tuple[list[str], Receipt]:
        """Return up to ``max_lines`` JSON records after the cursor, and their receipt.

        The receipt ends at the last record taken, so a backlog drains over
        intervals; ``--lines`` with ``--after-cursor`` would keep the NEWEST n and
        drop the oldest. With no cursor yet, seed from the newest record, ship nothing.
        """
        stored = self._store.get_cursor(self._group.name)
        if stored is None:
            self._seed()
            return ([], self._receipt("", ""))

        out = self._run(["--after-cursor", stored]) or ""
        records = [line for line in out.splitlines() if line.strip()]
        if not records:
            return ([], self._receipt(stored, stored))

        taken = records[:max_lines]
        to_cursor = ""
        for record_line in reversed(taken):
            to_cursor = self._cursor_of(record_line)
            if to_cursor:
                break
        if not to_cursor:
            # Every record we took is unparseable, so there is no cursor to
            # advance to. Do not move the position: re-reading is the correct
            # failure, losing the window is not.
            return (taken, self._receipt(stored, stored))
        return (taken, self._receipt(stored, to_cursor))

    def confirm_shipped(self, receipt: Receipt) -> bool:
        """Store the receipt's end cursor; refuse an empty one or another unit's."""
        cursor = receipt.to_position
        if not _is_own(receipt, self._group, self._group.unit):
            return False
        if not isinstance(cursor, str) or not cursor:
            return False
        self._store.set_cursor(self._group.name, self._group.unit, cursor)
        return True


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

    def _receipt(self, from_ts: str, to_ts: str) -> Receipt:
        return Receipt(
            self._group.name, self._group.container_name, None, from_ts, to_ts
        )

    def read_new_lines(self, max_lines: int) -> tuple[list[str], Receipt]:
        """Drain up to ``max_lines`` lines from the running subprocess stdout.

        ``from_ts`` is the stored last_ts (seeded to "now" on first run, same
        as ``DockerTailer``). ``to_ts`` is the timestamp of the last line
        received advanced by 1µs; equals ``from_ts`` when no lines arrive.
        Never raises.
        """
        from_ts = _docker_from_ts(self._group, self._store)
        if not self._ensure_running():
            return ([], self._receipt(from_ts, from_ts))

        raw_lines = self._drain_lines(max_lines)
        if not raw_lines:
            return ([], self._receipt(from_ts, from_ts))

        lines = raw_lines[: min(max_lines, MAX_LINES_PER_INTERVAL)]
        return (lines, self._receipt(from_ts, _last_ts(lines, from_ts)))

    def confirm_shipped(self, receipt: Receipt) -> bool:
        """Store the receipt's end ts; refuse one this group's container never issued."""
        return _confirm_docker_ts(self._group, self._store, receipt)

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
        from_ts = stored or datetime.now(UTC).strftime(_DOCKER_TS_FORMAT)
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
