"""Synchronous file tailer.

Reads new lines from the last-recorded byte position; rotation detected by
inode comparison. Caller runs this in a thread via ``asyncio.to_thread``.

Position is NOT advanced until the caller confirms the batch shipped - this
is the at-least-once delivery guarantee across restarts.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from collections.abc import Callable, Collection

from stormpulse.config import LogGroupConfig
from stormpulse.logging.positions import LogPositionStore
from stormpulse.logging.receipt import FileGeneration, Receipt, holds_back

logger = logging.getLogger(__name__)

MAX_LINES_PER_INTERVAL = 1000

_JOURNALCTL_BINARY = "journalctl"
_JOURNALCTL_TIMEOUT_SECONDS = 15
# Bytes of a file's head that name its generation alongside the inode.
_HEAD_BYTES = 4096


def is_own(receipt: Receipt, group: LogGroupConfig, source: str) -> bool:
    """True when ``receipt`` was issued for this group's current source."""
    return receipt.group == group.name and receipt.source == source


def consume_filtered(
    confirm: Callable[[Receipt], bool],
    receipt: Receipt,
    pending: Collection[Receipt] | None,
) -> bool:
    """Pull tailers: checkpoint a wholly filtered read unless a pending one may hold matches below it.

    Held while ``pending`` is unknown (None) or any receipt in it may carry
    matches below this read's end: their ACK, not this, moves past them.
    """
    if pending is None or any(holds_back(p, receipt) for p in pending):
        return False
    return confirm(receipt)


def _generation(fd: int, inode: int, upto: int) -> FileGeneration:
    """The generation of open file ``fd``: its inode and its first ``upto`` bytes (capped)."""
    head = os.pread(fd, min(_HEAD_BYTES, upto), 0)
    digest = hashlib.blake2b(head, digest_size=16).digest()
    return FileGeneration(inode, len(head), digest)


def _holds(fd: int, st: os.stat_result, gen: FileGeneration) -> bool:
    """True when open file ``fd`` (stat ``st``) is still generation ``gen``."""
    return _generation(fd, st.st_ino, gen.head_len) == gen


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

    def read_new_lines(
        self, max_lines: int, pending: Collection[Receipt] | None = None
    ) -> tuple[list[str], Receipt]:
        """Read up to ``max_lines`` new lines; return them and the read's receipt.

        The receipt's range runs from the start offset to just past the last
        line read; its generation names the file read. Nothing is stored until
        ``confirm_shipped`` is handed that receipt after the batch is ACKed.
        """
        path = self._group.source_path
        capped = min(max_lines, MAX_LINES_PER_INTERVAL)
        lines: list[str] = []
        start = 0
        try:
            # Start, read and generation all from one descriptor: a rename
            # between two opens would pair one file's offset with another.
            with open(path, "rb") as f:
                st = os.fstat(f.fileno())
                start = self._resume_point(f.fileno(), st)
                f.seek(start)
                while len(lines) < capped and (raw := f.readline()):
                    lines.append(raw.decode("utf-8", errors="replace"))
                end = f.tell()
                generation = _generation(f.fileno(), st.st_ino, end)
        except FileNotFoundError:
            return ([], self._receipt(None, 0, 0))
        except OSError as exc:
            logger.warning("Failed reading %s: %s", path, exc)
            return ([], self._receipt(None, start, start))
        return (lines, self._receipt(generation, start, end))

    def _resume_point(self, fd: int, st: os.stat_result) -> int:
        """The stored offset if it belongs to open file ``fd``'s generation, else 0.

        Another inode, a file shorter than the offset, or a head that no longer
        matches the stored one (copytruncate, regrown) is a new generation. A row
        stored without a head trusts its offset.
        """
        cp = self._store.checkpoint(self._group.name)
        if cp.inode is not None and cp.inode != st.st_ino:
            logger.info(
                "Log rotation detected for group %s (inode %s -> %s)",
                self._group.name,
                cp.inode,
                st.st_ino,
            )
            return 0
        held = cp.generation is None or _holds(fd, st, cp.generation)
        return cp.position if held and cp.position <= st.st_size else 0

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
        base = self._base_for(gen, end)
        if base is None or not start <= base < end:
            return False
        self._store.set(
            self._group.name, str(self._group.source_path), end, gen.inode, head=gen
        )
        return True

    def skip_filtered(
        self, receipt: Receipt, pending: Collection[Receipt] | None
    ) -> bool:
        """Consume a wholly filtered read; see ``consume_filtered``."""
        return consume_filtered(self.confirm_shipped, receipt, pending)

    def _own_range(self, receipt: Receipt) -> tuple[FileGeneration, int, int] | None:
        """The receipt's generation and byte range, or None if this tailer never issued it."""
        gen, start, end = receipt.generation, receipt.from_position, receipt.to_position
        if not is_own(receipt, self._group, str(self._group.source_path)):
            return None
        if gen is None or not isinstance(start, int) or not isinstance(end, int):
            return None
        return gen, start, end

    def _base_for(self, gen: FileGeneration, end: int) -> int | None:
        """The checkpoint a receipt of ``gen`` must cover.

        None if the path left ``gen`` or is short of ``end``.
        """
        try:
            with open(self._group.source_path, "rb") as f:
                st = os.fstat(f.fileno())
                if not _holds(f.fileno(), st, gen) or st.st_size < end:
                    return None
                return self._resume_point(f.fileno(), st)
        except OSError:
            return None


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

    def read_new_lines(
        self, max_lines: int, pending: Collection[Receipt] | None = None
    ) -> tuple[list[str], Receipt]:
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
        if not is_own(receipt, self._group, self._group.unit):
            return False
        if not isinstance(cursor, str) or not cursor:
            return False
        self._store.set_cursor(self._group.name, self._group.unit, cursor)
        return True

    def skip_filtered(
        self, receipt: Receipt, pending: Collection[Receipt] | None
    ) -> bool:
        """Consume a wholly filtered read; see ``consume_filtered``."""
        return consume_filtered(self.confirm_shipped, receipt, pending)
