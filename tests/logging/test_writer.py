"""Tests for PulseLogger."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from stormpulse.logging.writer import PulseLogger


def _read_lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln]


def test_info_writes_json_line(tmp_path: Path) -> None:
    log = tmp_path / "agent.log"
    pl = PulseLogger(log, "test-agent")
    pl.info("Hello", "connection", {"url": "wss://x"})
    entries = _read_lines(log)
    assert len(entries) == 1
    assert entries[0]["level"] == "INFO"
    assert entries[0]["message"] == "Hello"
    assert entries[0]["event_type"] == "connection"
    assert entries[0]["agent_id"] == "test-agent"
    assert entries[0]["detail"] == {"url": "wss://x"}


def test_multiple_levels(tmp_path: Path) -> None:
    log = tmp_path / "agent.log"
    pl = PulseLogger(log, "a")
    pl.info("i", "connection")
    pl.warning("w", "connection")
    pl.error("e", "error")
    entries = _read_lines(log)
    assert [e["level"] for e in entries] == ["INFO", "WARNING", "ERROR"]


def test_command_result_non_sensitive(tmp_path: Path) -> None:
    log = tmp_path / "agent.log"
    pl = PulseLogger(log, "a")
    pl.log_command_result("git_pull", success=True, duration_ms=150, sensitive=False)
    entries = _read_lines(log)
    assert len(entries) == 1
    assert entries[0]["event_type"] == "command"
    assert entries[0]["detail"]["command"] == "git_pull"
    assert entries[0]["detail"]["success"] is True
    assert entries[0]["detail"]["duration_ms"] == 150
    assert entries[0]["detail"]["sensitive"] is False


def test_command_result_sensitive_omits_output(tmp_path: Path) -> None:
    """Sensitive commands must not leak stdout/stderr into the log."""
    log = tmp_path / "agent.log"
    pl = PulseLogger(log, "a")
    pl.log_command_result(
        "garage_key_create", success=True, duration_ms=40, sensitive=True,
    )
    content = log.read_text()
    # The API surface doesn't take stdout/stderr, but also confirm
    # nothing secret-looking slipped in.
    assert "stdout" not in content
    assert "stderr" not in content
    entry = _read_lines(log)[0]
    assert entry["detail"]["sensitive"] is True
    assert "stdout" not in entry["detail"]
    assert "stderr" not in entry["detail"]


def test_command_result_failure_uses_warning_level(tmp_path: Path) -> None:
    log = tmp_path / "agent.log"
    pl = PulseLogger(log, "a")
    pl.log_command_result("x", success=False, duration_ms=10, sensitive=False)
    entry = _read_lines(log)[0]
    assert entry["level"] == "WARNING"


def test_command_result_with_sequence_id(tmp_path: Path) -> None:
    log = tmp_path / "agent.log"
    pl = PulseLogger(log, "a")
    pl.log_command_result(
        "x", success=True, duration_ms=10, sensitive=False, sequence_id="seq-1",
    )
    entry = _read_lines(log)[0]
    assert entry["detail"]["sequence_id"] == "seq-1"


def test_append_across_calls(tmp_path: Path) -> None:
    log = tmp_path / "agent.log"
    pl = PulseLogger(log, "a")
    pl.info("one", "connection")
    pl.info("two", "connection")
    pl.info("three", "connection")
    assert len(_read_lines(log)) == 3

def test_creates_log_file_if_missing(tmp_path: Path) -> None:
    log = tmp_path / "subdir" / "agent.log"
    log.parent.mkdir()
    pl = PulseLogger(log, "a")
    pl.info("hello", "connection")
    assert log.exists()
    assert len(_read_lines(log)) == 1

def test_unwritable_path_does_not_crash(tmp_path: Path) -> None:
    log = tmp_path / "agent.log"
    log.touch()
    log.chmod(0o444)  # read-only
    pl = PulseLogger(log, "a")
    pl.info("this should not crash", "connection")  # falls back to stdlib logger

def test_timestamp_format(tmp_path: Path) -> None:
    from datetime import datetime, timezone
    log = tmp_path / "agent.log"
    pl = PulseLogger(log, "a")
    pl.info("x", "connection")
    entry = _read_lines(log)[0]
    ts = entry["ts"]
    assert ts.endswith("Z")
    datetime.fromisoformat(ts.replace("Z", "+00:00"))  # must parse cleanly

def test_each_line_is_valid_json(tmp_path: Path) -> None:
    log = tmp_path / "agent.log"
    pl = PulseLogger(log, "a")
    for i in range(5):
        pl.info(f"msg{i}", "connection")
    for line in log.read_text().splitlines():
        obj = json.loads(line)  # each line independently parseable
        assert isinstance(obj, dict)