"""Tests for LogPositionStore."""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

from stormpulse.logging.positions import Checkpoint, LogPositionStore
from stormpulse.logging.receipt import FileGeneration


def test_get_unseen_group(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    assert store.get("storage") == (0, None)
    store.close()


def test_set_and_get_roundtrip(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set("storage", "/var/log/garage/access.log", 12345, 999)
    pos, inode = store.get("storage")
    assert pos == 12345
    assert inode == 999
    store.close()


def test_checkpoint_head_roundtrip_and_cleared_by_a_headless_set(
    tmp_path: Path,
) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    assert store.checkpoint("storage") == Checkpoint(0, None, None)
    store.set("storage", "/a.log", 300, 7, head=FileGeneration(7, 120, b"\x00digest"))
    assert store.checkpoint("storage") == Checkpoint(
        300, 7, FileGeneration(7, 120, b"\x00digest")
    )
    assert store.get("storage") == (300, 7)
    store.set("storage", "/a.log", 400, 7, head=FileGeneration(7, 64, b"other"))
    assert store.checkpoint("storage") == Checkpoint(
        400, 7, FileGeneration(7, 64, b"other")
    )
    # A head belongs to one generation: a set without one must not keep the old.
    store.set("storage", "/a.log", 0, 8)
    assert store.checkpoint("storage") == Checkpoint(0, 8, None)
    store.close()


def test_legacy_row_without_head_columns_reads_back_headless(tmp_path: Path) -> None:
    db = tmp_path / "pos.db"
    # The schema an agent wrote before heads were kept.
    with contextlib.closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(
            "CREATE TABLE log_positions (group_name TEXT PRIMARY KEY, source_type TEXT "
            "NOT NULL DEFAULT 'file', file_path TEXT, position INTEGER NOT NULL DEFAULT 0, "
            "inode INTEGER, last_ts TEXT, cursor TEXT, updated_at REAL NOT NULL)"
        )
        conn.execute(
            "INSERT INTO log_positions (group_name, file_path, position, inode, updated_at) "
            "VALUES ('storage', '/a.log', 500, 42, 0)"
        )
    store = LogPositionStore(db)
    assert store.checkpoint("storage") == Checkpoint(500, 42, None)
    assert store.get("storage") == (500, 42)
    store.close()


def test_inode_zero_roundtrip(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set("storage", "/a.log", 0, 0)
    pos, inode = store.get("storage")
    assert pos == 0
    assert inode == 0  # not None - zero is a valid inode
    store.close()


def test_position_zero_after_rotation(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set("storage", "/a.log", 5000, 42)
    store.set("storage", "/a.log", 0, 99)  # rotation reset
    pos, inode = store.get("storage")
    assert pos == 0
    assert inode == 99
    store.close()


def test_two_connections_same_db(tmp_path: Path) -> None:
    db = tmp_path / "pos.db"
    s1 = LogPositionStore(db)
    s2 = LogPositionStore(db)
    s1.set("storage", "/a.log", 100, 1)
    s2.set("pulse", "/b.log", 200, 2)
    assert s1.get("pulse") == (200, 2)
    assert s2.get("storage") == (100, 1)
    s1.close()
    s2.close()


def test_upsert_overwrites(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set("storage", "/path/a", 100, 1)
    store.set("storage", "/path/b", 200, 2)
    pos, inode = store.get("storage")
    assert pos == 200
    assert inode == 2
    store.close()


def test_multiple_groups_independent(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set("storage", "/a.log", 10, 1)
    store.set("pulse", "/b.log", 20, 2)
    assert store.get("storage") == (10, 1)
    assert store.get("pulse") == (20, 2)
    store.close()


def test_persistence_across_connections(tmp_path: Path) -> None:
    db = tmp_path / "pos.db"
    s1 = LogPositionStore(db)
    s1.set("storage", "/a.log", 500, 42)
    s1.close()

    s2 = LogPositionStore(db)
    assert s2.get("storage") == (500, 42)
    s2.close()


def test_get_docker_ts_unseen_returns_none(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    assert store.get_docker_ts("missing") is None
    store.close()


def test_set_and_get_docker_ts_roundtrip(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_docker_ts("web", "web", "2026-04-15T13:00:00.000000Z")
    assert store.get_docker_ts("web") == "2026-04-15T13:00:00.000000Z"
    store.set_docker_ts("web", "web", "2026-04-15T14:00:00.000000Z")
    assert store.get_docker_ts("web") == "2026-04-15T14:00:00.000000Z"
    store.close()


def test_file_and_docker_groups_coexist(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set("fgroup", "/var/log/x.log", 123, 456)
    store.set_docker_ts("dgroup", "web", "2026-04-15T13:00:00.000000Z")
    assert store.get("fgroup") == (123, 456)
    assert store.get_docker_ts("dgroup") == "2026-04-15T13:00:00.000000Z"
    # Cross-lookups return defaults
    assert store.get_docker_ts("fgroup") is None
    assert store.get("dgroup") == (0, None)
    store.close()


def test_nanosecond_cursors_normalized_on_init(tmp_path: Path) -> None:
    """Old nano-precision cursors get truncated to µs on startup."""
    store = LogPositionStore(tmp_path / "pos.db")
    # Simulate old agent writing nanosecond cursor directly
    store._conn.execute(
        "INSERT INTO log_positions (group_name, source_type, last_ts, updated_at) "
        "VALUES ('nano', 'docker', '2026-04-16T15:37:00.600193533Z', 0)"
    )
    store._conn.commit()
    store.close()
    # Re-open - _ensure_table should normalize
    store2 = LogPositionStore(tmp_path / "pos.db")
    assert store2.get_docker_ts("nano") == "2026-04-16T15:37:00.600193Z"
    store2.close()


def test_microsecond_cursors_untouched_on_init(tmp_path: Path) -> None:
    """Already-clean µs cursors are not mangled by the normalizer."""
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_docker_ts("clean", "web", "2026-04-16T15:37:00.600193Z")
    store.close()
    store2 = LogPositionStore(tmp_path / "pos.db")
    assert store2.get_docker_ts("clean") == "2026-04-16T15:37:00.600193Z"
    store2.close()
