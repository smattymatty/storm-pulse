"""Tests for LogShipper."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from stormpulse.config import LogGroupConfig
from stormpulse.logging.positions import LogPositionStore
from stormpulse.logging.shipper import LogShipper
from stormpulse.logging.tailer import DockerTailer, LogTailer


def _make_group(
    source_path: Path,
    *,
    parser: str = "stormpulse",
    filter_contains: str = "",
    max_batch: int = 50,
) -> LogGroupConfig:
    return LogGroupConfig(
        name="test",
        enabled=True,
        source_type="file",
        source_path=source_path,
        filter_contains=filter_contains,
        parser=parser,
        ship_interval_seconds=10.0,
        max_lines_per_batch=max_batch,
        retention_days=30,
    )


def _stormpulse_line(message: str) -> str:
    return json.dumps({
        "ts": "2026-04-10T13:00:00Z",
        "level": "INFO",
        "message": message,
        "event_type": "connection",
    })


def test_empty_source_returns_none(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "t.log"
    log.touch()
    group = _make_group(log)
    shipper = LogShipper(group, LogTailer(group, store))
    assert shipper.collect_batch() is None
    store.close()


def test_parses_valid_lines(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "t.log"
    log.write_text(_stormpulse_line("a") + "\n" + _stormpulse_line("b") + "\n")
    group = _make_group(log)
    shipper = LogShipper(group, LogTailer(group, store))
    batch = shipper.collect_batch()
    assert batch is not None
    assert len(batch.lines) == 2
    assert batch.dropped == 0
    assert batch.from_position == 0
    assert isinstance(batch.to_position, int)
    assert batch.to_position > 0
    store.close()


def test_malformed_lines_counted_as_dropped(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "t.log"
    log.write_text(_stormpulse_line("ok") + "\nnot json\n" + _stormpulse_line("ok2") + "\n")
    group = _make_group(log)
    shipper = LogShipper(group, LogTailer(group, store))
    batch = shipper.collect_batch()
    assert batch is not None
    assert len(batch.lines) == 2
    assert batch.dropped == 1
    store.close()


def test_filter_contains_skips_non_matching(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "t.log"
    log.write_text(
        _stormpulse_line("keep this") + "\n"
        + _stormpulse_line("drop this") + "\n"
    )
    group = _make_group(log, filter_contains="keep")
    shipper = LogShipper(group, LogTailer(group, store))
    batch = shipper.collect_batch()
    assert batch is not None
    assert len(batch.lines) == 1
    # Skipped lines are NOT counted as dropped (they're just not for us)
    assert batch.dropped == 0
    store.close()


def test_max_batch_limit_respected(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "t.log"
    log.write_text("".join(_stormpulse_line(f"m{i}") + "\n" for i in range(10)))
    group = _make_group(log, max_batch=3)
    shipper = LogShipper(group, LogTailer(group, store))
    batch = shipper.collect_batch()
    assert batch is not None
    assert len(batch.lines) == 3
    # Remaining lines should be available on next call after confirm
    assert isinstance(batch.to_position, int)
    shipper.tailer.confirm_shipped(batch.to_position)  # type: ignore[arg-type]
    batch2 = shipper.collect_batch()
    assert batch2 is not None
    assert len(batch2.lines) == 3
    store.close()


def test_unknown_parser_raises(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "t.log"
    group = LogGroupConfig(
        name="x", enabled=True, source_type="file", source_path=log,
        filter_contains="", parser="not_a_parser",
        ship_interval_seconds=10.0, max_lines_per_batch=5, retention_days=1,
    )
    with pytest.raises(ValueError):
        LogShipper(group, LogTailer(group, store))
    store.close()

def test_position_not_advanced_without_confirm(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "t.log"
    log.write_text(_stormpulse_line("a") + "\n")
    group = _make_group(log)
    shipper = LogShipper(group, LogTailer(group, store))
    batch1 = shipper.collect_batch()
    assert batch1 is not None
    # Without confirm, position in store is still 0
    assert store.get("test") == (0, None)
    # Re-reading returns the same lines again
    batch2 = shipper.collect_batch()
    assert batch2 is not None
    assert len(batch2.lines) == len(batch1.lines)
    store.close()

def test_all_filtered_returns_none(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "t.log"
    log.write_text(_stormpulse_line("irrelevant") + "\n")
    group = _make_group(log, filter_contains="needle")
    shipper = LogShipper(group, LogTailer(group, store))
    assert shipper.collect_batch() is None
    store.close()

def test_all_dropped_ships_drop_count(tmp_path: Path) -> None:
    """Unparseable source still ships dropped count so dashboard sees the signal."""
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "t.log"
    log.write_text("bad\nbad\nbad\n")
    group = _make_group(log)
    shipper = LogShipper(group, LogTailer(group, store))
    batch = shipper.collect_batch()
    assert batch is not None
    assert batch.lines == []
    assert batch.dropped == 3
    store.close()


def test_shipper_with_docker_tailer(tmp_path: Path) -> None:
    """Confirm LogShipper works when handed a DockerTailer — to_position
    is a string timestamp (not int) and confirm_shipped accepts it."""
    store = LogPositionStore(tmp_path / "pos.db")
    group = LogGroupConfig(
        name="web", enabled=True, source_type="docker",
        source_path=Path(""), filter_contains="", parser="docker_raw",
        ship_interval_seconds=10.0, max_lines_per_batch=50, retention_days=30,
        container_name="web", docker_binary="/usr/bin/docker",
    )
    store.set_docker_ts("web", "web", "2026-04-16T13:00:00.000000Z")
    tailer = DockerTailer(group, store)
    shipper = LogShipper(group, tailer)

    stdout = "2026-04-16T13:00:01.000000Z some log line\n"
    with patch("stormpulse.logging.tailer.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=stdout, stderr="",
        )
        batch = shipper.collect_batch()

    assert batch is not None
    assert len(batch.lines) == 1
    assert isinstance(batch.to_position, str)
    assert batch.to_position > "2026-04-16T13:00:01.000000Z"
    # Confirm round-trips through the store cleanly
    shipper.tailer.confirm_shipped(batch.to_position)  # type: ignore[arg-type]
    assert store.get_docker_ts("web") == batch.to_position
    store.close()