"""Tests for StreamingDockerTailer (long-lived ``docker logs --follow``)."""

from __future__ import annotations

import errno
import fcntl
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock, patch

from stormpulse.config import LogGroupConfig
from stormpulse.logging.positions import LogPositionStore
from stormpulse.logging.tailer import StreamingDockerTailer


def _make_docker_group(name: str = "web", container: str = "web") -> LogGroupConfig:
    return LogGroupConfig(
        name=name,
        enabled=True,
        source_type="docker_stream",
        source_path=Path(""),
        filter_contains="",
        parser="docker_raw",
        ship_interval_seconds=10.0,
        max_lines_per_batch=50,
        retention_days=30,
        container_name=container,
        docker_binary="/usr/bin/docker",
    )


@dataclass
class _FakeProc:
    """Stand-in for a subprocess.Popen with a real fd under stdout."""

    read_fd: int
    alive: bool = True
    terminated: bool = False
    killed: bool = False

    def __post_init__(self) -> None:
        self.stdout = MagicMock()
        self.stdout.fileno = MagicMock(return_value=self.read_fd)

    def poll(self) -> int | None:
        return None if self.alive else 0

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def terminate(self) -> None:
        self.terminated = True
        self.alive = False

    def kill(self) -> None:
        self.killed = True
        self.alive = False


def _make_pipe_nonblocking() -> tuple[int, int]:
    """Return a (read_fd, write_fd) pipe with the reader set non-blocking."""
    r, w = os.pipe()
    flags = fcntl.fcntl(r, fcntl.F_GETFL)
    fcntl.fcntl(r, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    return r, w


def _inject_fake_proc(
    tailer: StreamingDockerTailer, proc: _FakeProc,
) -> None:
    """Install ``proc`` as the tailer's running subprocess."""
    tailer._proc = proc  # type: ignore[assignment]
    tailer._buffer = b""


# ---------------------------------------------------------------------------
# Spawn arguments
# ---------------------------------------------------------------------------


def test_spawns_with_correct_args(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    group = _make_docker_group(container="my_web_1")
    store.set_docker_ts("web", "my_web_1", "2026-04-15T13:00:00.000000Z")
    tailer = StreamingDockerTailer(group, store)

    r, w = _make_pipe_nonblocking()
    fake = _FakeProc(read_fd=r)
    try:
        with patch(
            "stormpulse.logging.tailer.subprocess.Popen", return_value=fake,
        ) as mock_popen:
            tailer._ensure_running()
        args = mock_popen.call_args.args[0]
        kwargs = mock_popen.call_args.kwargs
        assert args == [
            "/usr/bin/docker", "logs",
            "--follow", "--timestamps",
            "--since", "2026-04-15T13:00:00.000000Z",
            "my_web_1",
        ]
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] == subprocess.STDOUT
        assert kwargs["shell"] is False
        assert kwargs["bufsize"] == 0
    finally:
        os.close(w)
        os.close(r)
        store.close()


def test_first_run_seeds_from_now(tmp_path: Path) -> None:
    """No stored ts → seed to a recent UTC timestamp and persist it."""
    store = LogPositionStore(tmp_path / "pos.db")
    group = _make_docker_group()
    tailer = StreamingDockerTailer(group, store)

    r, w = _make_pipe_nonblocking()
    fake = _FakeProc(read_fd=r)
    try:
        with patch(
            "stormpulse.logging.tailer.subprocess.Popen", return_value=fake,
        ), patch.object(tailer, "_READ_TIMEOUT_SECONDS", 0.05):
            lines, from_ts, to_ts = tailer.read_new_lines(max_lines=10)
    finally:
        os.close(w)
        os.close(r)
    assert lines == []
    assert from_ts.startswith("20")
    assert "1970" not in from_ts
    assert from_ts == to_ts
    # The seed must have been persisted so a subsequent run doesn't re-seed.
    assert store.get_docker_ts("web") == from_ts
    store.close()


# ---------------------------------------------------------------------------
# Drain behaviour
# ---------------------------------------------------------------------------


def test_drains_available_lines(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_docker_ts("web", "web", "2026-04-15T13:00:00.000000Z")
    group = _make_docker_group()
    tailer = StreamingDockerTailer(group, store)

    r, w = _make_pipe_nonblocking()
    fake = _FakeProc(read_fd=r)
    try:
        os.write(w, b"2026-04-15T13:00:01.000000000Z first\n")
        os.write(w, b"2026-04-15T13:00:02.000000000Z second\n")
        _inject_fake_proc(tailer, fake)
        with patch.object(tailer, "_READ_TIMEOUT_SECONDS", 0.2):
            lines = tailer._drain_lines(max_lines=10)
    finally:
        os.close(w)
        os.close(r)

    assert lines == [
        "2026-04-15T13:00:01.000000000Z first",
        "2026-04-15T13:00:02.000000000Z second",
    ]
    store.close()


def test_returns_empty_when_silent(tmp_path: Path) -> None:
    """Timeout elapses with nothing on the pipe → empty result, not error."""
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_docker_ts("web", "web", "2026-04-15T13:00:00.000000Z")
    group = _make_docker_group()
    tailer = StreamingDockerTailer(group, store)

    r, w = _make_pipe_nonblocking()
    fake = _FakeProc(read_fd=r)
    try:
        _inject_fake_proc(tailer, fake)
        with patch.object(tailer, "_READ_TIMEOUT_SECONDS", 0.1):
            started = time.monotonic()
            lines, from_ts, to_ts = tailer.read_new_lines(max_lines=10)
            elapsed = time.monotonic() - started
    finally:
        os.close(w)
        os.close(r)

    assert lines == []
    assert from_ts == to_ts == "2026-04-15T13:00:00.000000Z"
    # Must return within the timeout, not hang.
    assert elapsed < 1.0
    store.close()


def test_max_lines_respected(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_docker_ts("web", "web", "2026-04-15T13:00:00.000000Z")
    group = _make_docker_group()
    tailer = StreamingDockerTailer(group, store)

    r, w = _make_pipe_nonblocking()
    fake = _FakeProc(read_fd=r)
    try:
        for i in range(20):
            os.write(w, f"2026-04-15T13:00:{i:02d}.000000Z line {i}\n".encode())
        _inject_fake_proc(tailer, fake)
        with patch.object(tailer, "_READ_TIMEOUT_SECONDS", 0.2):
            lines = tailer._drain_lines(max_lines=5)
    finally:
        os.close(w)
        os.close(r)
    assert len(lines) == 5
    store.close()


def test_partial_line_buffered_across_calls(tmp_path: Path) -> None:
    """A chunk without a trailing newline must be held for the next call."""
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_docker_ts("web", "web", "2026-04-15T13:00:00.000000Z")
    group = _make_docker_group()
    tailer = StreamingDockerTailer(group, store)

    r, w = _make_pipe_nonblocking()
    fake = _FakeProc(read_fd=r)
    try:
        os.write(w, b"2026-04-15T13:00:01.000000Z par")
        _inject_fake_proc(tailer, fake)
        with patch.object(tailer, "_READ_TIMEOUT_SECONDS", 0.1):
            first = tailer._drain_lines(max_lines=10)
        os.write(w, b"tial\n")
        with patch.object(tailer, "_READ_TIMEOUT_SECONDS", 0.1):
            second = tailer._drain_lines(max_lines=10)
    finally:
        os.close(w)
        os.close(r)

    assert first == []
    assert second == ["2026-04-15T13:00:01.000000Z partial"]
    store.close()


# ---------------------------------------------------------------------------
# Process lifecycle
# ---------------------------------------------------------------------------


def test_detects_dead_process(tmp_path: Path) -> None:
    """poll() returning non-None → proc cleared, respawn scheduled."""
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_docker_ts("web", "web", "2026-04-15T13:00:00.000000Z")
    group = _make_docker_group()
    tailer = StreamingDockerTailer(group, store)

    r, w = _make_pipe_nonblocking()
    dead = _FakeProc(read_fd=r, alive=False)
    try:
        _inject_fake_proc(tailer, dead)
        # Prime the backoff so we don't attempt a respawn on this call.
        tailer._last_respawn_attempt = time.monotonic()
        running = tailer._ensure_running()
    finally:
        os.close(w)
        os.close(r)

    assert running is False
    assert tailer._proc is None
    store.close()


def test_respawn_backoff(tmp_path: Path) -> None:
    """Second call within backoff window must not invoke Popen again."""
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_docker_ts("web", "web", "2026-04-15T13:00:00.000000Z")
    group = _make_docker_group()
    tailer = StreamingDockerTailer(group, store)

    r1, w1 = _make_pipe_nonblocking()
    try:
        # First spawn fails → sets _last_respawn_attempt.
        with patch(
            "stormpulse.logging.tailer.subprocess.Popen",
            side_effect=FileNotFoundError,
        ) as mock_popen:
            assert tailer._ensure_running() is False
            # Second call within the 5s window should NOT call Popen again.
            assert tailer._ensure_running() is False
            assert mock_popen.call_count == 1
    finally:
        os.close(w1)
        os.close(r1)
    store.close()


def test_respawn_after_backoff(tmp_path: Path) -> None:
    """After the backoff window elapses, a respawn attempt is allowed."""
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_docker_ts("web", "web", "2026-04-15T13:00:00.000000Z")
    group = _make_docker_group()
    tailer = StreamingDockerTailer(group, store)

    r, w = _make_pipe_nonblocking()
    fake = _FakeProc(read_fd=r)
    try:
        with patch.object(tailer, "_RESPAWN_DELAY_SECONDS", 0.05), patch(
            "stormpulse.logging.tailer.subprocess.Popen",
            side_effect=[FileNotFoundError, fake],
        ) as mock_popen:
            assert tailer._ensure_running() is False
            time.sleep(0.08)
            assert tailer._ensure_running() is True
            assert mock_popen.call_count == 2
    finally:
        os.close(w)
        os.close(r)
    store.close()


# ---------------------------------------------------------------------------
# Cursor contract
# ---------------------------------------------------------------------------


def test_to_ts_advanced_past_last_line(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_docker_ts("web", "web", "2026-04-15T13:00:00.000000Z")
    group = _make_docker_group()
    tailer = StreamingDockerTailer(group, store)

    r, w = _make_pipe_nonblocking()
    fake = _FakeProc(read_fd=r)
    try:
        os.write(w, b"2026-04-15T13:23:51.766230288Z first\n")
        os.write(w, b"2026-04-15T13:23:53.100000000Z last\n")
        _inject_fake_proc(tailer, fake)
        with patch.object(tailer, "_READ_TIMEOUT_SECONDS", 0.2):
            lines, from_ts, to_ts = tailer.read_new_lines(max_lines=10)
    finally:
        os.close(w)
        os.close(r)

    assert len(lines) == 2
    assert from_ts == "2026-04-15T13:00:00.000000Z"
    # Advanced one microsecond past the last line's timestamp.
    assert to_ts == "2026-04-15T13:23:53.100001Z"
    store.close()


def test_confirm_shipped_persists(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    group = _make_docker_group()
    tailer = StreamingDockerTailer(group, store)
    tailer.confirm_shipped("2026-04-15T14:00:00.000000Z")
    assert store.get_docker_ts("web") == "2026-04-15T14:00:00.000000Z"
    store.close()


def test_position_not_advanced_without_confirm(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_docker_ts("web", "web", "2026-04-15T13:00:00.000000Z")
    group = _make_docker_group()
    tailer = StreamingDockerTailer(group, store)

    r, w = _make_pipe_nonblocking()
    fake = _FakeProc(read_fd=r)
    try:
        os.write(w, b"2026-04-15T13:23:51.000000Z new line\n")
        _inject_fake_proc(tailer, fake)
        with patch.object(tailer, "_READ_TIMEOUT_SECONDS", 0.2):
            tailer.read_new_lines(max_lines=10)
    finally:
        os.close(w)
        os.close(r)
    assert store.get_docker_ts("web") == "2026-04-15T13:00:00.000000Z"
    store.close()


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


def test_close_terminates_process(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    group = _make_docker_group()
    tailer = StreamingDockerTailer(group, store)

    r, w = _make_pipe_nonblocking()
    fake = _FakeProc(read_fd=r)
    try:
        _inject_fake_proc(tailer, fake)
        tailer.close()
    finally:
        os.close(w)
        os.close(r)
    assert fake.terminated is True
    assert tailer._proc is None
    store.close()


def test_close_escalates_to_kill_on_timeout(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    group = _make_docker_group()
    tailer = StreamingDockerTailer(group, store)

    r, w = _make_pipe_nonblocking()
    fake = _FakeProc(read_fd=r)

    call_count = {"n": 0}

    def slow_wait(timeout: float | None = None) -> int:
        call_count["n"] += 1
        # First wait (after terminate) times out; second wait (after kill) returns.
        if call_count["n"] == 1:
            raise subprocess.TimeoutExpired(cmd="docker", timeout=timeout or 0)
        return 0

    fake.wait = slow_wait  # type: ignore[method-assign]

    try:
        _inject_fake_proc(tailer, fake)
        tailer.close()
    finally:
        os.close(w)
        os.close(r)

    assert fake.terminated is True
    assert fake.killed is True
    store.close()


def test_close_is_noop_when_not_running(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    group = _make_docker_group()
    tailer = StreamingDockerTailer(group, store)
    # Should not raise.
    tailer.close()
    store.close()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_merges_stderr_into_stdout(tmp_path: Path) -> None:
    """``stderr=subprocess.STDOUT`` so container errors surface alongside stdout."""
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_docker_ts("web", "web", "2026-04-15T13:00:00.000000Z")
    group = _make_docker_group()
    tailer = StreamingDockerTailer(group, store)

    r, w = _make_pipe_nonblocking()
    fake = _FakeProc(read_fd=r)
    try:
        with patch(
            "stormpulse.logging.tailer.subprocess.Popen", return_value=fake,
        ) as mock_popen:
            tailer._ensure_running()
        assert mock_popen.call_args.kwargs["stderr"] == subprocess.STDOUT
    finally:
        os.close(w)
        os.close(r)
    store.close()


def test_docker_binary_missing(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_docker_ts("web", "web", "2026-04-15T13:00:00.000000Z")
    group = _make_docker_group()
    tailer = StreamingDockerTailer(group, store)

    with patch(
        "stormpulse.logging.tailer.subprocess.Popen",
        side_effect=FileNotFoundError,
    ):
        lines, from_ts, to_ts = tailer.read_new_lines(max_lines=10)

    assert lines == []
    assert from_ts == to_ts == "2026-04-15T13:00:00.000000Z"
    assert tailer._proc is None
    store.close()


def test_read_new_lines_never_raises(tmp_path: Path) -> None:
    """Any OSError on the pipe collapses to an empty result, not a crash."""
    store = LogPositionStore(tmp_path / "pos.db")
    store.set_docker_ts("web", "web", "2026-04-15T13:00:00.000000Z")
    group = _make_docker_group()
    tailer = StreamingDockerTailer(group, store)

    r, w = _make_pipe_nonblocking()
    fake = _FakeProc(read_fd=r)
    try:
        # Put bytes on the pipe so select() reports ready and os.read is called.
        os.write(w, b"anything\n")
        _inject_fake_proc(tailer, fake)
        with patch(
            "stormpulse.logging.tailer.os.read",
            side_effect=OSError(errno.EIO, "pipe error"),
        ), patch.object(tailer, "_READ_TIMEOUT_SECONDS", 0.2):
            lines, _, _ = tailer.read_new_lines(max_lines=10)
    finally:
        os.close(w)
        os.close(r)
    assert lines == []
    store.close()
