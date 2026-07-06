"""Tests for the rclone subprocess helpers: env building, stats parsing,
stderr capping. Streaming runs a real subprocess via /bin/sh."""

from __future__ import annotations

import asyncio
import signal
from typing import Any

import pytest

from stormpulse.rclone.config import RcloneConfig
from stormpulse.rclone.runner import (
    MAX_STDERR_TAIL_BYTES,
    build_env,
    reason_for_exit,
    run_rclone,
    run_rclone_streaming,
    stop_process,
    tail_capped,
)
from tests.rclone.helpers import REMOTE


def test_build_env_locks_out_config_file_and_carries_remotes() -> None:
    env = build_env(src=REMOTE, dst=REMOTE)
    assert env["RCLONE_CONFIG"] == "/dev/null"
    for name in ("SRC", "DST"):
        assert env[f"RCLONE_CONFIG_{name}_TYPE"] == "s3"
        assert env[f"RCLONE_CONFIG_{name}_ENDPOINT"] == REMOTE.endpoint
        assert env[f"RCLONE_CONFIG_{name}_SECRET_ACCESS_KEY"] == REMOTE.secret_access_key


def test_build_env_omits_undeclared_remote() -> None:
    env = build_env(src=REMOTE)
    assert "RCLONE_CONFIG_DST_TYPE" not in env


def test_build_env_never_inherits_agent_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Nothing secret in the agent's env may reach the subprocess, and a
    # stray RCLONE_* var must not silently reconfigure rclone.
    monkeypatch.setenv("STORM_AGENT_SECRET", "hunter2")
    monkeypatch.setenv("RCLONE_BWLIMIT", "1k")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = build_env(src=REMOTE)
    assert "STORM_AGENT_SECRET" not in env
    assert "RCLONE_BWLIMIT" not in env
    assert env["PATH"] == "/usr/bin:/bin"


def test_tail_capped_keeps_the_end() -> None:
    text = "x" * MAX_STDERR_TAIL_BYTES + "the actual error"
    capped = tail_capped(text)
    assert capped.endswith("the actual error")
    assert len(capped.encode()) <= MAX_STDERR_TAIL_BYTES


def test_reason_for_exit_names_known_codes_and_falls_back() -> None:
    assert reason_for_exit(7) == "fatal_error"
    assert reason_for_exit(42) == "rclone_exit_42"


@pytest.mark.asyncio
async def test_run_rclone_captures_output() -> None:
    config = RcloneConfig(enabled=True, binary_path="/bin/echo")
    code, stdout, stderr = await run_rclone(
        config, "hello", env={"PATH": "/bin"}, timeout=10
    )
    assert code == 0
    assert stdout.strip() == "hello"


@pytest.mark.asyncio
async def test_streaming_routes_stats_and_caps_tail() -> None:
    # Emits one stats line (routed to on_stats, kept out of the tail) and
    # one error line (kept in the tail).
    script = (
        "echo '{\"level\":\"info\",\"stats\":{\"bytes\":5,\"transfers\":1}}' >&2; "
        "echo 'boom: object failed' >&2; "
        "exit 7"
    )
    config = RcloneConfig(enabled=True, binary_path="/bin/sh")
    seen: list[dict[str, Any]] = []

    async def on_stats(stats: dict[str, Any]) -> None:
        seen.append(stats)

    code, tail = await run_rclone_streaming(
        config, "-c", script, env={"PATH": "/bin"}, on_stats=on_stats
    )
    assert code == 7
    assert seen == [{"bytes": 5, "transfers": 1}]
    assert "boom: object failed" in tail
    assert "stats" not in tail


@pytest.mark.asyncio
async def test_stats_callback_failure_never_aborts_the_transfer() -> None:
    # Two stats lines; the callback raises on the first. The run must
    # complete and still deliver the second.
    script = (
        "echo '{\"stats\":{\"bytes\":1}}' >&2; "
        "echo '{\"stats\":{\"bytes\":2}}' >&2; "
        "exit 0"
    )
    config = RcloneConfig(enabled=True, binary_path="/bin/sh")
    seen: list[dict[str, Any]] = []

    async def flaky(stats: dict[str, Any]) -> None:
        seen.append(stats)
        if len(seen) == 1:
            raise RuntimeError("relay hiccup")

    code, _ = await run_rclone_streaming(
        config, "-c", script, env={"PATH": "/bin"}, on_stats=flaky
    )
    assert code == 0
    assert seen == [{"bytes": 1}, {"bytes": 2}]


@pytest.mark.asyncio
async def test_stop_process_terminates_gracefully() -> None:
    proc = await asyncio.create_subprocess_exec(
        "/bin/sh", "-c", "sleep 30", env={"PATH": "/bin"}
    )
    await stop_process(proc)
    assert proc.returncode == -signal.SIGTERM


@pytest.mark.asyncio
async def test_stop_process_kills_a_term_ignoring_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "stormpulse.rclone.runner._TERMINATE_GRACE_SECONDS", 0.2
    )
    # The loop keeps the shell itself alive (a bare `sleep 30` would be
    # exec'd by sh, dropping the trap); the echo proves the trap is set
    # before the signal is sent.
    proc = await asyncio.create_subprocess_exec(
        "/bin/sh",
        "-c",
        'trap "" TERM; echo ready; while true; do sleep 1; done',
        stdout=asyncio.subprocess.PIPE,
        env={"PATH": "/bin"},
    )
    assert proc.stdout is not None
    await proc.stdout.readline()
    await stop_process(proc)
    assert proc.returncode == -signal.SIGKILL
