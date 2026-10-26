"""SQLite-backed log position store.

Shares the database file with the NonceStore. File-based log groups persist
last-read byte position, inode and head (length + digest), so rotation and an
in-place truncation are both detectable; Docker-based groups persist the
timestamp of the last line received.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from stormpulse.logging.receipt import FileGeneration


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """A file group's stored resume point and the generation it belongs to.

    ``generation`` is None for an unseen group or a row written before heads were kept.
    """

    position: int
    inode: int | None
    generation: FileGeneration | None


class LogPositionStore:
    """Persist position state per log group in SQLite."""

    def __init__(self, db_path: Path) -> None:
        self._conn = sqlite3.connect(
            str(db_path),
            timeout=5.0,
            check_same_thread=False,
        )
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._lock = threading.Lock()
            self._ensure_table()
        except Exception:
            # _ensure_table() can raise on schema migration / disk errors.
            # Close the connection we just opened so we don't leak it; the
            # OS would reap it on agent exit, but explicit is cleaner.
            self._conn.close()
            raise

    def _ensure_table(self) -> None:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS log_positions ("
            "  group_name  TEXT PRIMARY KEY,"
            "  source_type TEXT NOT NULL DEFAULT 'file',"
            "  file_path   TEXT,"
            "  position    INTEGER NOT NULL DEFAULT 0,"
            "  inode       INTEGER,"
            "  last_ts     TEXT,"
            "  updated_at  REAL NOT NULL"
            ")"
        )
        for col, definition in [
            ("source_type", "TEXT NOT NULL DEFAULT 'file'"),
            ("last_ts", "TEXT"),
            # Journald cursors get their own column rather than sharing
            # last_ts: a cursor is an opaque token, and the timestamp
            # normalization below rewrites values that merely LOOK like
            # timestamps.
            ("cursor", "TEXT"),
            ("head_len", "INTEGER"),
            ("head_digest", "BLOB"),
        ]:
            try:
                self._conn.execute(  # skylos: ignore[SKY-D211] col/definition from the hardcoded list above
                    f"ALTER TABLE log_positions ADD COLUMN {col} {definition}"
                )
            except sqlite3.OperationalError:
                pass
        # Normalize any nanosecond-precision cursors left by older agent
        # versions. Docker's --since only accepts microsecond precision;
        # nanosecond values silently return empty results, stalling the
        # log loop.
        self._conn.execute("""
            UPDATE log_positions
            SET last_ts = SUBSTR(last_ts, 1, INSTR(last_ts, '.') + 6) || 'Z'
            WHERE last_ts IS NOT NULL
              AND last_ts LIKE '%.%Z'
              AND LENGTH(last_ts) - INSTR(last_ts, '.') > 7
        """)
        self._conn.commit()

    def get(self, group: str) -> tuple[int, int | None]:
        """Return (position, inode) for a file group. (0, None) if unseen."""
        with self._lock:
            row = self._conn.execute(
                "SELECT position, inode FROM log_positions WHERE group_name = ?",
                (group,),
            ).fetchone()
        if row is None:
            return (0, None)
        return (int(row[0]), int(row[1]) if row[1] is not None else None)

    def checkpoint(self, group: str) -> Checkpoint:
        """Return the file group's position, inode and generation. Unseen: (0, None, None)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT position, inode, head_len, head_digest "
                "FROM log_positions WHERE group_name = ?",
                (group,),
            ).fetchone()
        if row is None:
            return Checkpoint(0, None, None)
        position, inode, head_len, head_digest = row
        if inode is None:
            return Checkpoint(int(position), None, None)
        generation = (
            None
            if head_len is None or head_digest is None
            else FileGeneration(int(inode), int(head_len), bytes(head_digest))
        )
        return Checkpoint(int(position), int(inode), generation)

    def set(
        self,
        group: str,
        file_path: str,
        position: int,
        inode: int,
        *,
        head: tuple[int, bytes] | None = None,
    ) -> None:
        """Upsert a file group's position, inode and head; no head clears the stored one."""
        head_len, head_digest = head if head is not None else (None, None)
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO log_positions "
                "  (group_name, source_type, file_path, position, inode, "
                "   head_len, head_digest, updated_at) "
                "VALUES (?, 'file', ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(group_name) DO UPDATE SET "
                "  source_type='file', "
                "  file_path=excluded.file_path, "
                "  position=excluded.position, "
                "  inode=excluded.inode, "
                "  head_len=excluded.head_len, "
                "  head_digest=excluded.head_digest, "
                "  updated_at=excluded.updated_at",
                (group, file_path, position, inode, head_len, head_digest, time.time()),
            )

    def get_docker_ts(self, group: str) -> str | None:
        """Return last_ts for a docker group, or None if unseen."""
        with self._lock:
            row = self._conn.execute(
                "SELECT last_ts FROM log_positions WHERE group_name = ?",
                (group,),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return str(row[0])

    def set_docker_ts(self, group: str, container_name: str, last_ts: str) -> None:
        """Upsert last_ts for a docker group."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO log_positions "
                "  (group_name, source_type, file_path, last_ts, updated_at) "
                "VALUES (?, 'docker', ?, ?, ?) "
                "ON CONFLICT(group_name) DO UPDATE SET "
                "  source_type='docker', "
                "  file_path=excluded.file_path, "
                "  last_ts=excluded.last_ts, "
                "  updated_at=excluded.updated_at",
                (group, container_name, last_ts, time.time()),
            )

    def get_cursor(self, group: str) -> str | None:
        """Return the stored journal cursor for a group, or None if unseen."""
        with self._lock:
            row = self._conn.execute(
                "SELECT cursor FROM log_positions WHERE group_name = ?",
                (group,),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return str(row[0])

    def set_cursor(self, group: str, unit: str, cursor: str) -> None:
        """Upsert the journal cursor for a group."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO log_positions "
                "  (group_name, source_type, file_path, cursor, updated_at) "
                "VALUES (?, 'journald', ?, ?, ?) "
                "ON CONFLICT(group_name) DO UPDATE SET "
                "  source_type='journald', "
                "  file_path=excluded.file_path, "
                "  cursor=excluded.cursor, "
                "  updated_at=excluded.updated_at",
                (group, unit, cursor, time.time()),
            )

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._conn.close()
