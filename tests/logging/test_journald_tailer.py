"""Tests for JournaldTailer.

The failure this class has to avoid is losing a window: a backlog larger than
one batch must drain over successive intervals, never be skipped, and a
journalctl that cannot run must leave the stored cursor exactly where it was.
"""

from __future__ import annotations

import io
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

from stormpulse.config import LogGroupConfig
from stormpulse.logging.positions import LogPositionStore
from stormpulse.logging.receipt import Receipt
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
    return json.dumps(
        {
            "__CURSOR": cursor,
            "__REALTIME_TIMESTAMP": str(ts_us),
            "MESSAGE": message,
        }
    )


def _receipt(to_cursor: str) -> Receipt:
    return Receipt("guard", "example.service", None, "s=1;i=9", to_cursor)


class _FakeProc:
    """A journalctl stand-in for ``Popen``: ``stdout`` yields records until killed."""

    def __init__(
        self, records: list[str], rc: int = 0, stderr: str = "", delay: float = 0.0
    ) -> None:
        self._records, self._rc, self._delay = records, rc, delay
        self.returncode: int | None = None
        self.killed = False
        self.pulled = 0
        self.stderr = io.StringIO(stderr)
        self.stdout = self._lines()

    def _lines(self) -> Iterator[str]:
        for record in self._records:
            time.sleep(self._delay)
            if self.killed:
                return
            self.pulled += 1
            yield record + "\n"
        self.returncode = self._rc

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            self.returncode = self._rc
        return self.returncode

    def __enter__(self) -> _FakeProc:
        return self

    def __exit__(self, *exc: object) -> None:
        self.wait()


def _popen(records: list[str] | str = "", rc: int = 0, stderr: str = "", **kw: Any):
    lines = records.splitlines() if isinstance(records, str) else records
    return patch(
        "stormpulse.logging.tailer.subprocess.Popen",
        return_value=_FakeProc(lines, rc, stderr, **kw),
    )


def test_first_run_seeds_from_newest_and_ships_nothing(tmp_path: Path) -> None:
    """Without the seed, a first start would replay the unit's whole journal."""
    store = LogPositionStore(tmp_path / "pos.db")
    tailer = JournaldTailer(_make_group(), store)

    with _popen(_record("s=1;i=9", "old line")) as popen:
        lines, receipt = tailer.read_new_lines(10)

    assert lines == []
    assert store.get_cursor("guard") == "s=1;i=9"
    assert "--lines" in popen.call_args[0][0]


def test_an_empty_journal_leaves_the_group_unseeded(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    tailer = JournaldTailer(_make_group(), store)

    with _popen():
        tailer.read_new_lines(10)

    assert store.get_cursor("guard") is None


def test_reads_after_the_stored_cursor(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_cursor("guard", "example.service", "s=1;i=9")
    tailer = JournaldTailer(_make_group(), store)

    with _popen(_record("s=1;i=10", "new line")) as popen:
        lines, receipt = tailer.read_new_lines(10)
        from_cursor, to_cursor = receipt.from_position, receipt.to_position

    argv = popen.call_args[0][0]
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

    out = [_record(f"s=1;i={i}", f"line {i}") for i in (10, 11, 12)]
    with _popen(out) as popen:
        lines, receipt = tailer.read_new_lines(2)
        to_cursor = receipt.to_position

    assert "--lines" not in popen.call_args[0][0]
    assert [json.loads(line)["MESSAGE"] for line in lines] == ["line 10", "line 11"]
    assert to_cursor == "s=1;i=11", "cursor must stop at the last line actually taken"


def test_a_backlog_is_read_one_batch_deep_and_journalctl_stopped(
    tmp_path: Path,
) -> None:
    """A read costs one batch however deep the backlog: a burst's backlog once
    took seven seconds a read and the group never caught up."""
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_cursor("guard", "example.service", "s=1;i=9")
    tailer = JournaldTailer(_make_group(), store)

    backlog = [_record(f"s=1;i={i}", f"line {i}") for i in range(10, 10_010)]
    with _popen(backlog) as popen:
        lines, receipt = tailer.read_new_lines(2)
    proc = popen.return_value

    assert len(lines) == 2
    assert receipt.to_position == "s=1;i=11"
    assert proc.pulled == 2, "journalctl was read past the batch"
    assert proc.killed, "journalctl was left printing the rest of the backlog"


def test_a_read_never_exceeds_the_per_interval_cap(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_cursor("guard", "example.service", "s=1;i=9")
    tailer = JournaldTailer(_make_group(), store)

    backlog = [_record(f"s=1;i={i}", f"line {i}") for i in range(10, 1_510)]
    with _popen(backlog):
        lines, _ = tailer.read_new_lines(5_000)

    assert len(lines) == 1_000


def test_confirm_shipped_persists_the_cursor(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    tailer = JournaldTailer(_make_group(), store)

    assert tailer.confirm_shipped(_receipt("s=1;i=11"))

    assert store.get_cursor("guard") == "s=1;i=11"


def test_confirm_shipped_ignores_an_empty_cursor(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_cursor("guard", "example.service", "s=1;i=9")
    tailer = JournaldTailer(_make_group(), store)

    assert not tailer.confirm_shipped(_receipt(""))

    assert store.get_cursor("guard") == "s=1;i=9", (
        "an empty cursor must not erase position"
    )


def test_unparseable_records_do_not_advance_the_cursor(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_cursor("guard", "example.service", "s=1;i=9")
    tailer = JournaldTailer(_make_group(), store)

    with _popen("not json\nalso not json\n"):
        lines, receipt = tailer.read_new_lines(10)
        from_cursor, to_cursor = receipt.from_position, receipt.to_position

    assert len(lines) == 2
    assert to_cursor == from_cursor == "s=1;i=9", "re-reading beats losing the window"


def test_a_missing_journalctl_is_an_empty_batch_not_a_crash(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_cursor("guard", "example.service", "s=1;i=9")
    tailer = JournaldTailer(_make_group(), store)

    with patch(
        "stormpulse.logging.tailer.subprocess.Popen", side_effect=FileNotFoundError
    ):
        lines, receipt = tailer.read_new_lines(10)
        from_cursor, to_cursor = receipt.from_position, receipt.to_position

    assert lines == []
    assert from_cursor == to_cursor == "s=1;i=9"


def test_a_timeout_keeps_what_was_read_and_kills_journalctl(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_cursor("guard", "example.service", "s=1;i=9")
    tailer = JournaldTailer(_make_group(), store)

    slow = [_record(f"s=1;i={i}", f"line {i}") for i in (10, 11, 12)]
    with (
        patch("stormpulse.logging.tailer._JOURNALCTL_TIMEOUT_SECONDS", 0.05),
        _popen(slow, delay=0.04) as popen,
    ):
        lines, receipt = tailer.read_new_lines(10)
        to_cursor = receipt.to_position

    assert popen.return_value.killed
    assert [json.loads(line)["MESSAGE"] for line in lines] == ["line 10"], (
        "a timed-out read keeps what it got: the receipt ends at the last record taken"
    )
    assert to_cursor == "s=1;i=10"


def test_a_nonzero_exit_is_an_empty_batch_not_a_crash(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_cursor("guard", "example.service", "s=1;i=9")
    tailer = JournaldTailer(_make_group(), store)

    with _popen(stderr="No such unit", rc=1):
        lines, receipt = tailer.read_new_lines(10)
        to_cursor = receipt.to_position

    assert lines == []
    assert to_cursor == "s=1;i=9"


def test_the_unit_is_passed_to_journalctl(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_cursor("svc", "my-daemon.service", "s=1;i=9")
    tailer = JournaldTailer(_make_group("svc", "my-daemon.service"), store)

    with _popen() as popen:
        tailer.read_new_lines(10)

    argv = popen.call_args[0][0]
    assert argv[argv.index("--unit") + 1] == "my-daemon.service"


def test_confirm_shipped_refuses_another_groups_or_units_cursor(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_cursor("guard", "example.service", "s=1;i=9")
    tailer = JournaldTailer(_make_group(), store)

    assert not tailer.confirm_shipped(
        Receipt("other", "example.service", None, "", "s=1;i=11")
    )
    assert not tailer.confirm_shipped(
        Receipt("guard", "other.service", None, "", "s=1;i=11")
    )
    assert store.get_cursor("guard") == "s=1;i=9"
