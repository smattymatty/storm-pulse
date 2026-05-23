"""Tests for DockerTailer."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from stormpulse.config import LogGroupConfig
from stormpulse.logging.positions import LogPositionStore
from stormpulse.logging.tailer import MAX_LINES_PER_INTERVAL, DockerTailer


def _make_docker_group(name: str = "web", container: str = "web") -> LogGroupConfig:
    return LogGroupConfig(
        name=name,
        enabled=True,
        source_type="docker",
        source_path=Path(""),
        filter_contains="",
        parser="docker_raw",
        ship_interval_seconds=10.0,
        max_lines_per_batch=50,
        retention_days=30,
        container_name=container,
        docker_binary="/usr/bin/docker",
    )


def _mk_result(stdout: str = "", stderr: str = "", rc: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=rc, stdout=stdout, stderr=stderr,
    )


def test_first_run_uses_now_not_epoch(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    group = _make_docker_group()
    tailer = DockerTailer(group, store)

    with patch("stormpulse.logging.tailer.subprocess.run") as mock_run:
        mock_run.return_value = _mk_result(stdout="")
        tailer.read_new_lines(max_lines=10)
        args = mock_run.call_args.args[0]
        since_idx = args.index("--since")
        since = args[since_idx + 1]
    # Must NOT be epoch - should be a recent timestamp starting with 20xx
    assert since.startswith("20")
    assert "1970" not in since
    store.close()


def test_subsequent_run_uses_stored_ts(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    group = _make_docker_group()
    store.set_docker_ts("web", "web", "2026-04-15T13:00:00.000000Z")
    tailer = DockerTailer(group, store)

    with patch("stormpulse.logging.tailer.subprocess.run") as mock_run:
        mock_run.return_value = _mk_result(stdout="")
        _, from_ts, _ = tailer.read_new_lines(max_lines=10)
    assert from_ts == "2026-04-15T13:00:00.000000Z"
    args = mock_run.call_args.args[0]
    assert "--since" in args
    assert args[args.index("--since") + 1] == "2026-04-15T13:00:00.000000Z"
    store.close()


def test_docker_binary_missing(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    group = _make_docker_group()
    store.set_docker_ts("web", "web", "2026-04-15T13:00:00.000000Z")
    tailer = DockerTailer(group, store)

    with patch("stormpulse.logging.tailer.subprocess.run", side_effect=FileNotFoundError):
        lines, from_ts, to_ts = tailer.read_new_lines(max_lines=10)
    assert lines == []
    assert from_ts == to_ts == "2026-04-15T13:00:00.000000Z"
    store.close()


def test_timeout_returns_empty(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    group = _make_docker_group()
    store.set_docker_ts("web", "web", "2026-04-15T13:00:00.000000Z")
    tailer = DockerTailer(group, store)

    with patch(
        "stormpulse.logging.tailer.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="docker", timeout=30),
    ):
        lines, _, _ = tailer.read_new_lines(max_lines=10)
    assert lines == []
    store.close()


def test_nonzero_exit_returns_empty(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    group = _make_docker_group()
    store.set_docker_ts("web", "web", "2026-04-15T13:00:00.000000Z")
    tailer = DockerTailer(group, store)

    with patch("stormpulse.logging.tailer.subprocess.run") as mock_run:
        mock_run.return_value = _mk_result(stderr="No such container: web", rc=1)
        lines, from_ts, to_ts = tailer.read_new_lines(max_lines=10)
    assert lines == []
    assert from_ts == to_ts
    store.close()


def test_to_ts_is_one_tick_past_last_line(tmp_path: Path) -> None:
    """Docker's ``--since`` is inclusive, so the stored cursor must be one
    tick past the last line or the same line re-ships forever."""
    store = LogPositionStore(tmp_path / "pos.db")
    group = _make_docker_group()
    store.set_docker_ts("web", "web", "2026-04-15T13:00:00.000000Z")
    tailer = DockerTailer(group, store)

    stdout = (
        "2026-04-15T13:23:51.766230288Z first line\n"
        "2026-04-15T13:23:52.100000000Z second line\n"
        "2026-04-15T13:23:53.100000000Z third line\n"
    )
    with patch("stormpulse.logging.tailer.subprocess.run") as mock_run:
        mock_run.return_value = _mk_result(stdout=stdout)
        lines, from_ts, to_ts = tailer.read_new_lines(max_lines=10)
    assert len(lines) == 3
    assert from_ts == "2026-04-15T13:00:00.000000Z"
    # Last line was ...53.100000000Z; cursor advances one microsecond past it.
    assert to_ts == "2026-04-15T13:23:53.100001Z"
    store.close()


def test_confirm_shipped_persists(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    group = _make_docker_group()
    tailer = DockerTailer(group, store)
    tailer.confirm_shipped("2026-04-15T14:00:00.000000Z")
    assert store.get_docker_ts("web") == "2026-04-15T14:00:00.000000Z"
    store.close()


def test_position_not_advanced_without_confirm(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    group = _make_docker_group()
    store.set_docker_ts("web", "web", "2026-04-15T13:00:00.000000Z")
    tailer = DockerTailer(group, store)

    stdout = "2026-04-15T13:23:51.000000Z new line\n"
    with patch("stormpulse.logging.tailer.subprocess.run") as mock_run:
        mock_run.return_value = _mk_result(stdout=stdout)
        tailer.read_new_lines(max_lines=10)
    assert store.get_docker_ts("web") == "2026-04-15T13:00:00.000000Z"
    store.close()


def test_max_lines_cap(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    group = _make_docker_group()
    store.set_docker_ts("web", "web", "2026-04-15T13:00:00.000000Z")
    tailer = DockerTailer(group, store)

    stdout = "".join(
        f"2026-04-15T13:00:00.{i:06d}Z line {i}\n"
        for i in range(MAX_LINES_PER_INTERVAL + 50)
    )
    with patch("stormpulse.logging.tailer.subprocess.run") as mock_run:
        mock_run.return_value = _mk_result(stdout=stdout)
        lines, _, _ = tailer.read_new_lines(max_lines=MAX_LINES_PER_INTERVAL + 50)
    assert len(lines) == MAX_LINES_PER_INTERVAL
    store.close()


def test_docker_command_args(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    group = _make_docker_group(container="my_web_1")
    store.set_docker_ts("web", "my_web_1", "2026-04-15T13:00:00.000000Z")
    tailer = DockerTailer(group, store)

    with patch("stormpulse.logging.tailer.subprocess.run") as mock_run:
        mock_run.return_value = _mk_result(stdout="")
        tailer.read_new_lines(max_lines=10)
    args = mock_run.call_args.args[0]
    kwargs = mock_run.call_args.kwargs
    assert args == [
        "/usr/bin/docker", "logs",
        "--since", "2026-04-15T13:00:00.000000Z",
        "--timestamps",
        "my_web_1",
    ]
    assert kwargs.get("shell") is False
    store.close()


def test_cursor_does_not_stall_on_boundary_line(tmp_path: Path) -> None:
    """Regression: before the fix, re-calling ``--since <last_ts>`` would
    re-ship the boundary line forever because Docker's ``--since`` is
    inclusive. With the one-tick advance, the second call's ``from_ts``
    is strictly past the shipped line.
    """
    store = LogPositionStore(tmp_path / "pos.db")
    group = _make_docker_group()
    store.set_docker_ts("web", "web", "2026-04-16T15:00:00.000000000Z")
    tailer = DockerTailer(group, store)

    stuck_line = "2026-04-16T15:37:00.600193533Z single line\n"
    with patch("stormpulse.logging.tailer.subprocess.run") as mock_run:
        mock_run.return_value = _mk_result(stdout=stuck_line)
        _, _, to_ts = tailer.read_new_lines(max_lines=10)
        tailer.confirm_shipped(to_ts)

    # Now the stored cursor must be strictly past the line we shipped.
    stored = store.get_docker_ts("web")
    assert stored is not None
    assert stored > "2026-04-16T15:37:00.600193Z"
    assert stored == "2026-04-16T15:37:00.600194Z"

    # Next call passes this new cursor to docker; the shipped line is
    # excluded by --since so we get nothing back.
    with patch("stormpulse.logging.tailer.subprocess.run") as mock_run:
        mock_run.return_value = _mk_result(stdout="")
        lines, from_ts, to_ts2 = tailer.read_new_lines(max_lines=10)
        args = mock_run.call_args.args[0]
    assert lines == []
    assert from_ts == "2026-04-16T15:37:00.600194Z"
    assert args[args.index("--since") + 1] == "2026-04-16T15:37:00.600194Z"
    store.close()


def test_advance_nanos_microsecond_precision(tmp_path: Path) -> None:
    """Cursor advances by 1µs (Docker only accepts microsecond precision)."""
    from stormpulse.logging.tailer import _advance_nanos
    # Nanosecond input gets truncated to µs then bumped
    assert _advance_nanos("2026-04-16T15:37:00.600193533Z") == (
        "2026-04-16T15:37:00.600194Z"
    )
    # Microsecond input bumped directly
    assert _advance_nanos("2026-04-16T15:37:00.600193Z") == (
        "2026-04-16T15:37:00.600194Z"
    )
    # µs overflow rolls into seconds
    assert _advance_nanos("2026-04-16T15:37:00.999999Z") == (
        "2026-04-16T15:37:01.000000Z"
    )
    # No fractional part
    assert _advance_nanos("2026-04-16T15:37:00Z") == (
        "2026-04-16T15:37:00.000001Z"
    )


def test_empty_output_does_not_advance(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    group = _make_docker_group()
    store.set_docker_ts("web", "web", "2026-04-15T13:00:00.000000Z")
    tailer = DockerTailer(group, store)

    with patch("stormpulse.logging.tailer.subprocess.run") as mock_run:
        mock_run.return_value = _mk_result(stdout="")
        lines, from_ts, to_ts = tailer.read_new_lines(max_lines=10)
    assert lines == []
    assert from_ts == to_ts == "2026-04-15T13:00:00.000000Z"
    store.close()
