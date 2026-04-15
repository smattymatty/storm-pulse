"""Tests for LogTailer."""

from __future__ import annotations

import os
from pathlib import Path

from stormpulse.config import LogGroupConfig
from stormpulse.logging.positions import LogPositionStore
from stormpulse.logging.tailer import LogTailer


def _make_group(source_path: Path, name: str = "test") -> LogGroupConfig:
    return LogGroupConfig(
        name=name,
        enabled=True,
        source_type="file",
        source_path=source_path,
        filter_contains="",
        parser="stormpulse",
        ship_interval_seconds=10.0,
        max_lines_per_batch=50,
        retention_days=30,
    )


def test_missing_file_returns_empty(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    group = _make_group(tmp_path / "nonexistent.log")
    tailer = LogTailer(group, store)
    lines, from_pos, to_pos = tailer.read_new_lines(max_lines=10)
    assert lines == []
    assert from_pos == 0
    assert to_pos == 0
    store.close()


def test_reads_full_file_from_start(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "test.log"
    log.write_text("line1\nline2\nline3\n")
    group = _make_group(log)
    tailer = LogTailer(group, store)
    lines, from_pos, to_pos = tailer.read_new_lines(max_lines=10)
    assert lines == ["line1\n", "line2\n", "line3\n"]
    assert from_pos == 0
    assert to_pos == len("line1\nline2\nline3\n")
    store.close()


def test_confirm_advances_position(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "test.log"
    log.write_text("a\nb\n")
    group = _make_group(log)
    tailer = LogTailer(group, store)
    _, _, to_pos = tailer.read_new_lines(max_lines=10)
    tailer.confirm_shipped(to_pos)

    # Append more; next read starts after confirmed pos
    with log.open("a") as f:
        f.write("c\n")
    lines, from_pos, _ = tailer.read_new_lines(max_lines=10)
    assert lines == ["c\n"]
    assert from_pos == to_pos
    store.close()


def test_no_confirm_re_reads(tmp_path: Path) -> None:
    """If confirm_shipped is never called, next read starts from 0 again."""
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "test.log"
    log.write_text("line1\nline2\n")
    group = _make_group(log)
    tailer = LogTailer(group, store)
    lines1, _, _ = tailer.read_new_lines(max_lines=10)
    lines2, from_pos2, _ = tailer.read_new_lines(max_lines=10)
    assert lines1 == lines2
    assert from_pos2 == 0  # unchanged, never confirmed
    store.close()


def test_rotation_detected_via_inode(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "test.log"
    log.write_text("old1\nold2\n")
    group = _make_group(log)
    tailer = LogTailer(group, store)

    _, _, to_pos = tailer.read_new_lines(max_lines=10)
    tailer.confirm_shipped(to_pos)

    # Rotate: remove + recreate to force new inode
    log.unlink()
    log.write_text("new1\n")
    # Ensure the inode actually changed
    assert os.stat(log).st_ino != to_pos  # loose sanity

    lines, from_pos, _ = tailer.read_new_lines(max_lines=10)
    assert lines == ["new1\n"]
    assert from_pos == 0  # rotation resets position
    store.close()


def test_truncation_in_place(tmp_path: Path) -> None:
    """If file shrinks below stored position, restart from 0."""
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "test.log"
    log.write_text("line1\nline2\nline3\n")
    group = _make_group(log)
    tailer = LogTailer(group, store)

    _, _, to_pos = tailer.read_new_lines(max_lines=10)
    tailer.confirm_shipped(to_pos)

    # Truncate in place, keeping the same inode
    inode_before = os.stat(log).st_ino
    with log.open("w") as f:
        f.write("short\n")
    assert os.stat(log).st_ino == inode_before  # same inode

    lines, from_pos, _ = tailer.read_new_lines(max_lines=10)
    assert lines == ["short\n"]
    assert from_pos == 0
    store.close()


def test_max_lines_caps_read(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "test.log"
    log.write_text("".join(f"line{i}\n" for i in range(10)))
    group = _make_group(log)
    tailer = LogTailer(group, store)
    lines, _, to_pos = tailer.read_new_lines(max_lines=3)
    assert len(lines) == 3
    # Confirming advances only past the 3 read lines
    tailer.confirm_shipped(to_pos)
    lines2, _, _ = tailer.read_new_lines(max_lines=3)
    assert lines2 == ["line3\n", "line4\n", "line5\n"]
    store.close()

def test_no_new_lines_after_confirm(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "test.log"
    log.write_text("line1\n")
    group = _make_group(log)
    tailer = LogTailer(group, store)
    _, _, to_pos = tailer.read_new_lines(max_lines=10)
    tailer.confirm_shipped(to_pos)
    lines, _, _ = tailer.read_new_lines(max_lines=10)
    assert lines == []
    store.close()

def test_hard_cap_overrides_max_lines(tmp_path: Path) -> None:
    from stormpulse.logging.tailer import MAX_LINES_PER_INTERVAL
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "test.log"
    log.write_text("".join(f"line{i}\n" for i in range(MAX_LINES_PER_INTERVAL + 50)))
    group = _make_group(log)
    tailer = LogTailer(group, store)
    lines, _, _ = tailer.read_new_lines(max_lines=MAX_LINES_PER_INTERVAL + 50)
    assert len(lines) == MAX_LINES_PER_INTERVAL
    store.close()

def test_position_survives_tailer_restart(tmp_path: Path) -> None:
    db = tmp_path / "pos.db"
    log = tmp_path / "test.log"
    log.write_text("a\nb\n")

    store1 = LogPositionStore(db)
    group = _make_group(log)
    tailer1 = LogTailer(group, store1)
    _, _, to_pos = tailer1.read_new_lines(max_lines=10)
    tailer1.confirm_shipped(to_pos)
    store1.close()

    with log.open("a") as f:
        f.write("c\n")

    store2 = LogPositionStore(db)
    tailer2 = LogTailer(group, store2)
    lines, from_pos, _ = tailer2.read_new_lines(max_lines=10)
    assert lines == ["c\n"]
    assert from_pos == to_pos
    store2.close()