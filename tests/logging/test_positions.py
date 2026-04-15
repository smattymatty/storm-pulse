"""Tests for LogPositionStore."""

from __future__ import annotations

from pathlib import Path

from stormpulse.logging.positions import LogPositionStore


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

def test_inode_zero_roundtrip(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set("storage", "/a.log", 0, 0)
    pos, inode = store.get("storage")
    assert pos == 0
    assert inode == 0  # not None — zero is a valid inode
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


def test_get_docker_ts_unseen_returns_none(tmp_path):
    from stormpulse.logging.positions import LogPositionStore
    store = LogPositionStore(tmp_path / "pos.db")
    assert store.get_docker_ts("missing") is None
    store.close()


def test_set_and_get_docker_ts_roundtrip(tmp_path):
    from stormpulse.logging.positions import LogPositionStore
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_docker_ts("web", "web", "2026-04-15T13:00:00.000000Z")
    assert store.get_docker_ts("web") == "2026-04-15T13:00:00.000000Z"
    store.set_docker_ts("web", "web", "2026-04-15T14:00:00.000000Z")
    assert store.get_docker_ts("web") == "2026-04-15T14:00:00.000000Z"
    store.close()


def test_file_and_docker_groups_coexist(tmp_path):
    from stormpulse.logging.positions import LogPositionStore
    store = LogPositionStore(tmp_path / "pos.db")
    store.set("fgroup", "/var/log/x.log", 123, 456)
    store.set_docker_ts("dgroup", "web", "2026-04-15T13:00:00.000000Z")
    assert store.get("fgroup") == (123, 456)
    assert store.get_docker_ts("dgroup") == "2026-04-15T13:00:00.000000Z"
    # Cross-lookups return defaults
    assert store.get_docker_ts("fgroup") is None
    assert store.get("dgroup") == (0, None)
    store.close()
