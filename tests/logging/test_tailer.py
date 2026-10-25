"""Tests for LogTailer."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any

import pytest

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
    )


def test_missing_file_returns_empty(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    group = _make_group(tmp_path / "nonexistent.log")
    tailer = LogTailer(group, store)
    lines, receipt = tailer.read_new_lines(max_lines=10)
    from_pos, to_pos = receipt.from_position, receipt.to_position
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
    lines, receipt = tailer.read_new_lines(max_lines=10)
    from_pos, to_pos = receipt.from_position, receipt.to_position
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
    _, receipt = tailer.read_new_lines(max_lines=10)
    to_pos = receipt.to_position
    tailer.confirm_shipped(receipt)

    # Append more; next read starts after confirmed pos
    with log.open("a") as f:
        f.write("c\n")
    lines, receipt = tailer.read_new_lines(max_lines=10)
    from_pos = receipt.from_position
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
    lines1, receipt = tailer.read_new_lines(max_lines=10)
    lines2, receipt = tailer.read_new_lines(max_lines=10)
    from_pos2 = receipt.from_position
    assert lines1 == lines2
    assert from_pos2 == 0  # unchanged, never confirmed
    store.close()


def test_rotation_detected_via_inode(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "test.log"
    log.write_text("old1\nold2\n")
    group = _make_group(log)
    tailer = LogTailer(group, store)

    _, receipt = tailer.read_new_lines(max_lines=10)
    tailer.confirm_shipped(receipt)

    # Rotate: remove + recreate to force new inode
    log.unlink()
    log.write_text("new1\n")

    lines, receipt = tailer.read_new_lines(max_lines=10)
    from_pos = receipt.from_position
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

    _, receipt = tailer.read_new_lines(max_lines=10)
    tailer.confirm_shipped(receipt)

    # Truncate in place, keeping the same inode
    inode_before = os.stat(log).st_ino
    with log.open("w") as f:
        f.write("short\n")
    assert os.stat(log).st_ino == inode_before  # same inode

    lines, receipt = tailer.read_new_lines(max_lines=10)
    from_pos = receipt.from_position
    assert lines == ["short\n"]
    assert from_pos == 0
    store.close()


def test_max_lines_caps_read(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "test.log"
    log.write_text("".join(f"line{i}\n" for i in range(10)))
    group = _make_group(log)
    tailer = LogTailer(group, store)
    lines, receipt = tailer.read_new_lines(max_lines=3)
    assert len(lines) == 3
    # Confirming advances only past the 3 read lines
    tailer.confirm_shipped(receipt)
    lines2, receipt = tailer.read_new_lines(max_lines=3)
    assert lines2 == ["line3\n", "line4\n", "line5\n"]
    store.close()


def test_no_new_lines_after_confirm(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "test.log"
    log.write_text("line1\n")
    group = _make_group(log)
    tailer = LogTailer(group, store)
    _, receipt = tailer.read_new_lines(max_lines=10)
    tailer.confirm_shipped(receipt)
    lines, receipt = tailer.read_new_lines(max_lines=10)
    assert lines == []
    store.close()


def test_hard_cap_overrides_max_lines(tmp_path: Path) -> None:
    from stormpulse.logging.tailer import MAX_LINES_PER_INTERVAL

    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "test.log"
    log.write_text("".join(f"line{i}\n" for i in range(MAX_LINES_PER_INTERVAL + 50)))
    group = _make_group(log)
    tailer = LogTailer(group, store)
    lines, receipt = tailer.read_new_lines(max_lines=MAX_LINES_PER_INTERVAL + 50)
    assert len(lines) == MAX_LINES_PER_INTERVAL
    store.close()


def test_position_survives_tailer_restart(tmp_path: Path) -> None:
    db = tmp_path / "pos.db"
    log = tmp_path / "test.log"
    log.write_text("a\nb\n")

    store1 = LogPositionStore(db)
    group = _make_group(log)
    tailer1 = LogTailer(group, store1)
    _, receipt = tailer1.read_new_lines(max_lines=10)
    to_pos = receipt.to_position
    tailer1.confirm_shipped(receipt)
    store1.close()

    with log.open("a") as f:
        f.write("c\n")

    store2 = LogPositionStore(db)
    tailer2 = LogTailer(group, store2)
    lines, receipt = tailer2.read_new_lines(max_lines=10)
    from_pos = receipt.from_position
    assert lines == ["c\n"]
    assert from_pos == to_pos
    store2.close()


def test_confirm_refuses_a_receipt_from_a_renamed_away_file(
    tmp_path: Path, store: LogPositionStore
) -> None:
    log = tmp_path / "test.log"
    log.write_text("old1\nold2\n")
    tailer = LogTailer(_make_group(log), store)
    _, old = tailer.read_new_lines(max_lines=10)
    log.rename(tmp_path / "test.log.1")
    log.write_text("new1\nnew2\nnew3\n")

    assert not tailer.confirm_shipped(old)
    assert store.get("test") == (0, None)


def test_confirm_refuses_a_receipt_once_truncation_rewrote_the_head(
    tmp_path: Path, store: LogPositionStore
) -> None:
    """copytruncate keeps the inode; the rewritten head names the new generation."""
    log = tmp_path / "test.log"
    log.write_text("old1\nold2\n")
    tailer = LogTailer(_make_group(log), store)
    _, old = tailer.read_new_lines(max_lines=10)
    log.write_text("new1\nnew2\nnew3\n")  # same inode, longer than the old read

    assert not tailer.confirm_shipped(old)
    assert store.get("test") == (0, None)


def test_confirm_refuses_a_receipt_past_the_end_of_a_shrunk_file(
    tmp_path: Path, store: LogPositionStore
) -> None:
    """Past the hashed head, a file cut below the receipt's end is a new generation."""
    log = tmp_path / "test.log"
    body = "".join(f"line{i:05d}\n" for i in range(600))  # 6600 bytes, head 4096
    log.write_text(body)
    tailer = LogTailer(_make_group(log), store)
    _, receipt = tailer.read_new_lines(max_lines=1000)
    with log.open("r+") as f:
        f.truncate(5000)

    assert not tailer.confirm_shipped(receipt)
    assert store.get("test") == (0, None)


def test_confirm_refuses_another_groups_receipt(
    tmp_path: Path, store: LogPositionStore
) -> None:
    log = tmp_path / "test.log"
    log.write_text("a\nb\n")
    mine = LogTailer(_make_group(log, name="mine"), store)
    other = LogTailer(_make_group(log, name="other"), store)
    _, foreign = other.read_new_lines(max_lines=10)

    assert not mine.confirm_shipped(foreign)
    assert store.get("mine") == (0, None)


def test_confirm_refuses_a_receipt_for_another_source_path(
    tmp_path: Path, store: LogPositionStore
) -> None:
    """A group repointed at a new path keeps its name; the old path's receipt is not its."""
    old_log, new_log = tmp_path / "old.log", tmp_path / "new.log"
    old_log.write_text("a\nb\n")
    os.link(old_log, new_log)  # same inode and bytes: only the path differs
    _, receipt = LogTailer(_make_group(old_log), store).read_new_lines(max_lines=10)

    assert not LogTailer(_make_group(new_log), store).confirm_shipped(receipt)
    assert store.get("test") == (0, None)


def test_confirm_stores_the_inode_the_receipt_read(
    tmp_path: Path, store: LogPositionStore
) -> None:
    log = tmp_path / "test.log"
    log.write_text("a\nb\n")
    tailer = LogTailer(_make_group(log), store)
    _, receipt = tailer.read_new_lines(max_lines=10)

    assert tailer.confirm_shipped(receipt)
    assert store.get("test") == (4, os.stat(log).st_ino)


def test_confirm_refuses_a_range_that_starts_past_the_checkpoint(
    tmp_path: Path, store: LogPositionStore
) -> None:
    """Storing it would skip the unconfirmed bytes between checkpoint and start."""
    log = tmp_path / "test.log"
    log.write_text("a\nb\n")
    tailer = LogTailer(_make_group(log), store)
    _, receipt = tailer.read_new_lines(max_lines=10)

    assert not tailer.confirm_shipped(dataclasses.replace(receipt, from_position=2))
    assert store.get("test") == (0, None)


def test_confirm_after_truncation_starts_the_new_generation_at_zero(
    tmp_path: Path, store: LogPositionStore
) -> None:
    """A checkpoint past the end of a truncated file is the old generation's."""
    log = tmp_path / "test.log"
    log.write_text("old1\nold2\nold3\n")
    tailer = LogTailer(_make_group(log), store)
    assert tailer.confirm_shipped(tailer.read_new_lines(max_lines=10)[1])
    log.write_text("new1\nnew2\n")  # same inode, shorter than the checkpoint
    lines, receipt = tailer.read_new_lines(max_lines=1)
    assert lines == ["new1\n"]

    assert tailer.confirm_shipped(receipt)
    assert tailer.read_new_lines(max_lines=10)[0] == ["new2\n"]


def test_confirm_after_rename_starts_the_new_generation_at_zero(
    tmp_path: Path, store: LogPositionStore
) -> None:
    """A checkpoint on another inode is the old generation's, whatever its offset."""
    log = tmp_path / "test.log"
    log.write_text("old1\n")
    tailer = LogTailer(_make_group(log), store)
    assert tailer.confirm_shipped(tailer.read_new_lines(max_lines=10)[1])
    log.rename(tmp_path / "test.log.1")
    log.write_text("new1\nnew2\nnew3\n")  # longer than the old checkpoint
    lines, receipt = tailer.read_new_lines(max_lines=1)
    assert lines == ["new1\n"]

    assert tailer.confirm_shipped(receipt)
    assert tailer.read_new_lines(max_lines=10)[0] == ["new2\n", "new3\n"]


def test_confirm_refuses_a_duplicate_ack(
    tmp_path: Path, store: LogPositionStore
) -> None:
    """A range ending at the checkpoint is already stored: refused, not re-applied."""
    log = tmp_path / "test.log"
    log.write_text("a\nb\n")
    tailer = LogTailer(_make_group(log), store)
    _, receipt = tailer.read_new_lines(max_lines=10)
    assert tailer.confirm_shipped(receipt)

    assert not tailer.confirm_shipped(receipt)
    assert store.get("test") == (4, os.stat(log).st_ino)


@pytest.mark.parametrize(
    "forge",
    [
        pytest.param({"generation": None}, id="no-generation"),
        pytest.param(
            {"from_position": "c1", "to_position": "c2"}, id="cursor-positions"
        ),
    ],
)
def test_confirm_refuses_a_receipt_no_file_read_issues(
    tmp_path: Path, store: LogPositionStore, forge: dict[str, Any]
) -> None:
    """Own group and path, but no file read yields it: refused, never raised."""
    log = tmp_path / "test.log"
    log.write_text("a\nb\n")
    tailer = LogTailer(_make_group(log), store)
    _, receipt = tailer.read_new_lines(max_lines=10)

    assert not tailer.confirm_shipped(dataclasses.replace(receipt, **forge))
    assert store.get("test") == (0, None)


def test_a_failed_read_claims_no_range(tmp_path: Path, store: LogPositionStore) -> None:
    """A path that stats but will not open (a directory) reads nothing and covers nothing."""
    source = tmp_path / "test.log"
    source.mkdir()
    tailer = LogTailer(_make_group(source), store)

    lines, receipt = tailer.read_new_lines(max_lines=10)

    assert lines == []
    assert (receipt.from_position, receipt.to_position) == (0, 0)
    assert not tailer.confirm_shipped(receipt)
