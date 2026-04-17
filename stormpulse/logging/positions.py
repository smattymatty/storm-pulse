"""SQLite-backed log position store.

Shares the same database file as the NonceStore. For file-based log
groups, each group's last-read byte position and the file's inode are
persisted so the agent can resume tailing across restarts and detect
rotation. For Docker-based log groups, the timestamp of the last line
received is persisted instead.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


class LogPositionStore:
    """Persist position state per log group in SQLite."""

    def __init__(self, db_path: Path) -> None:
        self._conn = sqlite3.connect(
            str(db_path), timeout=5.0, check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.Lock()
        self._ensure_table()

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
        ]:
            try:
                self._conn.execute(
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

    def set(self, group: str, file_path: str, position: int, inode: int) -> None:
        """Upsert the stored position and inode for a file group."""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO log_positions "
                "  (group_name, source_type, file_path, position, inode, updated_at) "
                "VALUES (?, 'file', ?, ?, ?, ?) "
                "ON CONFLICT(group_name) DO UPDATE SET "
                "  source_type='file', "
                "  file_path=excluded.file_path, "
                "  position=excluded.position, "
                "  inode=excluded.inode, "
                "  updated_at=excluded.updated_at",
                (group, file_path, position, inode, time.time()),
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

    def close(self) -> None:
        """Close the database connection."""
        with self._lock:
            self._conn.close()
