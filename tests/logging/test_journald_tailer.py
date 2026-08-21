"""Tests for JournaldTailer.

The failure this class has to avoid is losing a window: a backlog larger than
one batch must drain over successive intervals, never be skipped, and a
journalctl that cannot run must leave the stored cursor exactly where it was.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from stormpulse.config import LogGroupConfig
from stormpulse.logging.positions import LogPositionStore
from stormpulse.logging.tailer import JournaldTailer


def _make_group(name: str = "guard", unit: str = "example.service") -> LogGroupConfig:
    return LogGroupConfig(
        name=name,
        enabled=True,
        source_type="journald",
        source_path=Path(""),
        filter_contains="",
        parser="journald",
        ship_interval_seconds=10.0,
        max_lines_per_batch=2,
        unit=unit,
    )


def _record(cursor: str, message: str, ts_us: int = 1_755_000_000_000_000) -> str:
    return json.dumps({
        "__CURSOR": cursor,
        "__REALTIME_TIMESTAMP": str(ts_us),
        "MESSAGE": message,
    })


def _mk_result(stdout: str = "", stderr: str = "", rc: int = 0):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=stdout, stderr=stderr)


def test_first_run_seeds_from_newest_and_ships_nothing(tmp_path: Path) -> None:
    """Without the seed, a first start would replay the unit's whole journal."""
    store = LogPositionStore(tmp_path / "pos.db")
    tailer = JournaldTailer(_make_group(), store)

    with patch("stormpulse.logging.tailer.subprocess.run") as run:
        run.return_value = _mk_result(_record("s=1;i=9", "old line") + "\n")
        lines, _, _ = tailer.read_new_lines(10)

    assert lines == []
    assert store.get_cursor("guard") == "s=1;i=9"
    assert "--lines" in run.call_args[0][0]


def test_an_empty_journal_leaves_the_group_unseeded(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    tailer = JournaldTailer(_make_group(), store)

    with patch("stormpulse.logging.tailer.subprocess.run") as run:
        run.return_value = _mk_result("")
        tailer.read_new_lines(10)

    assert store.get_cursor("guard") is None


def test_reads_after_the_stored_cursor(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_cursor("guard", "example.service", "s=1;i=9")
    tailer = JournaldTailer(_make_group(), store)

    with patch("stormpulse.logging.tailer.subprocess.run") as run:
        run.return_value = _mk_result(_record("s=1;i=10", "new line") + "\n")
        lines, from_cursor, to_cursor = tailer.read_new_lines(10)

    argv = run.call_args[0][0]
    assert "--after-cursor" in argv
    assert argv[argv.index("--after-cursor") + 1] == "s=1;i=9"
    assert len(lines) == 1
    assert from_cursor == "s=1;i=9"
    assert to_cursor == "s=1;i=10"


def test_a_backlog_drains_oldest_first_over_intervals(tmp_path: Path) -> None:
    """The whole reason --lines is not passed alongside --after-cursor: it
    returns the NEWEST n, which would skip the oldest of a backlog."""
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_cursor("guard", "example.service", "s=1;i=9")
    tailer = JournaldTailer(_make_group(), store)  # max_lines_per_batch is 2

    out = "\n".join(_record(f"s=1;i={i}", f"line {i}") for i in (10, 11, 12)) + "\n"
    with patch("stormpulse.logging.tailer.subprocess.run") as run:
        run.return_value = _mk_result(out)
        lines, _, to_cursor = tailer.read_new_lines(2)

    assert "--lines" not in run.call_args[0][0]
    assert [json.loads(line)["MESSAGE"] for line in lines] == ["line 10", "line 11"]
    assert to_cursor == "s=1;i=11", "cursor must stop at the last line actually taken"


def test_confirm_shipped_persists_the_cursor(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    tailer = JournaldTailer(_make_group(), store)

    tailer.confirm_shipped("s=1;i=11")

    assert store.get_cursor("guard") == "s=1;i=11"


def test_confirm_shipped_ignores_an_empty_cursor(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_cursor("guard", "example.service", "s=1;i=9")
    tailer = JournaldTailer(_make_group(), store)

    tailer.confirm_shipped("")

    assert store.get_cursor("guard") == "s=1;i=9", "an empty cursor must not erase position"


def test_unparseable_records_do_not_advance_the_cursor(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_cursor("guard", "example.service", "s=1;i=9")
    tailer = JournaldTailer(_make_group(), store)

    with patch("stormpulse.logging.tailer.subprocess.run") as run:
        run.return_value = _mk_result("not json\nalso not json\n")
        lines, from_cursor, to_cursor = tailer.read_new_lines(10)

    assert len(lines) == 2
    assert to_cursor == from_cursor == "s=1;i=9", "re-reading beats losing the window"


def test_a_missing_journalctl_is_an_empty_batch_not_a_crash(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_cursor("guard", "example.service", "s=1;i=9")
    tailer = JournaldTailer(_make_group(), store)

    with patch("stormpulse.logging.tailer.subprocess.run", side_effect=FileNotFoundError):
        lines, from_cursor, to_cursor = tailer.read_new_lines(10)

    assert lines == []
    assert from_cursor == to_cursor == "s=1;i=9"


def test_a_timeout_is_an_empty_batch_not_a_crash(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_cursor("guard", "example.service", "s=1;i=9")
    tailer = JournaldTailer(_make_group(), store)

    with patch(
        "stormpulse.logging.tailer.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="journalctl", timeout=15),
    ):
        lines, _, to_cursor = tailer.read_new_lines(10)

    assert lines == []
    assert to_cursor == "s=1;i=9"


def test_a_nonzero_exit_is_an_empty_batch_not_a_crash(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_cursor("guard", "example.service", "s=1;i=9")
    tailer = JournaldTailer(_make_group(), store)

    with patch("stormpulse.logging.tailer.subprocess.run") as run:
        run.return_value = _mk_result(stderr="No such unit", rc=1)
        lines, _, to_cursor = tailer.read_new_lines(10)

    assert lines == []
    assert to_cursor == "s=1;i=9"


def test_the_unit_is_passed_to_journalctl(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_cursor("svc", "my-daemon.service", "s=1;i=9")
    tailer = JournaldTailer(_make_group("svc", "my-daemon.service"), store)

    with patch("stormpulse.logging.tailer.subprocess.run") as run:
        run.return_value = _mk_result("")
        tailer.read_new_lines(10)

    argv = run.call_args[0][0]
    assert argv[argv.index("--unit") + 1] == "my-daemon.service"
