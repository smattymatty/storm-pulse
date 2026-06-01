"""Integration tests - real WebSocket server, real auth, real protocol parsing."""

from __future__ import annotations

import asyncio
import json
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from websockets.asyncio.client import connect as real_connect
from websockets.asyncio.server import ServerConnection, serve

from stormpulse.agent import Agent
from stormpulse.auth import NonceStore, generate_nonce
from stormpulse.protocol import CommandResultPayload
from tests.helpers import (
    AGENT_ID,
    FAKE_METRICS,
    PULSE_TOKEN,
    SECRET,
    build_config,
    make_failed_result,
    make_successful_result,
    sign_command_request,
    sign_command_sequence,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plain_connect(url: str, **kwargs):  # type: ignore[no-untyped-def]
    """Wrap websockets connect to strip the ssl kwarg for plain ws:// tests."""
    kwargs.pop("ssl", None)
    return real_connect(url, **kwargs)


async def _wait_for_register(
    ws: ServerConnection, *, timeout: float = 2.0
) -> dict[str, Any]:
    """Receive messages until a register envelope arrives."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise TimeoutError("No register message received")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        data: dict[str, Any] = json.loads(raw)
        if data["type"] == "register":
            return data


async def _collect_results(
    ws: ServerConnection,
    *,
    count: int | None = None,
    timeout: float = 2.0,
) -> list[dict[str, Any]]:
    """Collect command.result envelopes, filtering out heartbeats/metrics."""
    results: list[dict[str, Any]] = []
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except (TimeoutError, Exception):
            break
        data = json.loads(raw)
        if data["type"] == "command.result":
            results.append(data)
            if count is not None and len(results) >= count:
                break
    return results


async def _drain_non_results(
    ws: ServerConnection,
    *,
    timeout: float = 0.5,
) -> list[dict[str, Any]]:
    """Collect any command.result messages within timeout, filtering others."""
    return await _collect_results(ws, timeout=timeout)


def _exec_side_effect_factory(
    fail_commands: set[str] | None = None,
) -> object:
    """Return a side_effect function for execute_command that fails specified commands."""
    fail = fail_commands or set()

    def side_effect(
        name: str,
        config: object,
        request_id: str,
        sequence_id: str | None = None,
        *,
        registry: object,
        runtime_params: dict[str, str] | None = None,
    ) -> CommandResultPayload:
        if name in fail:
            return make_failed_result(
                command=name,
                request_id=request_id,
                sequence_id=sequence_id,
            )
        return make_successful_result(
            command=name,
            request_id=request_id,
            sequence_id=sequence_id,
        )

    return side_effect


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_lifecycle(tmp_path: Path, free_port: int) -> None:
    """Connect → register → command.request → result → shutdown."""
    config = build_config(tmp_path, free_port)
    store = NonceStore(tmp_path / "nonces.db")
    shutdown = asyncio.Event()
    server_done = asyncio.Event()

    register_data: dict[str, Any] = {}
    result_data: dict[str, Any] = {}

    async def handler(ws: ServerConnection) -> None:
        reg = await _wait_for_register(ws)
        register_data.update(reg)

        await ws.send(sign_command_request("git_pull"))

        results = await _collect_results(ws, count=1, timeout=2.0)
        if results:
            result_data.update(results[0])

        server_done.set()

    with (
        patch("stormpulse.agent.reconnect.connect", side_effect=_plain_connect),
        patch(
            "stormpulse.agent.dispatch.execute_command",
            side_effect=_exec_side_effect_factory(),
        ),
        patch("stormpulse.agent.loops.collect_metrics", return_value=FAKE_METRICS),
    ):
        async with serve(handler, "localhost", free_port):
            agent = Agent(
                config, SECRET, store, MagicMock(spec=ssl.SSLContext), shutdown
            )
            agent_task = asyncio.create_task(agent.run())

            await asyncio.wait_for(server_done.wait(), timeout=5.0)
            shutdown.set()
            await asyncio.wait_for(agent_task, timeout=5.0)

    store.close()

    assert register_data["type"] == "register"
    assert register_data["agent_id"] == AGENT_ID
    assert register_data["payload"]["pulse_token"] == PULSE_TOKEN

    assert result_data["type"] == "command.result"
    assert result_data["payload"]["success"] is True
    assert result_data["payload"]["command"] == "git_pull"


@pytest.mark.asyncio
async def test_bad_hmac_rejected(tmp_path: Path, free_port: int) -> None:
    """Command with wrong HMAC produces no result."""
    config = build_config(tmp_path, free_port)
    store = NonceStore(tmp_path / "nonces.db")
    shutdown = asyncio.Event()
    server_done = asyncio.Event()
    stray_results: list[dict[str, Any]] = []

    async def handler(ws: ServerConnection) -> None:
        await _wait_for_register(ws)

        # Send a command with a garbage HMAC
        bad_msg = sign_command_request(
            "git_pull", secret=b"wrong-secret-not-the-real-one!!"
        )
        await ws.send(bad_msg)

        stray_results.extend(await _drain_non_results(ws, timeout=0.5))
        server_done.set()

    mock_exec = MagicMock()

    with (
        patch("stormpulse.agent.reconnect.connect", side_effect=_plain_connect),
        patch("stormpulse.agent.dispatch.execute_command", mock_exec),
        patch("stormpulse.agent.loops.collect_metrics", return_value=FAKE_METRICS),
    ):
        async with serve(handler, "localhost", free_port):
            agent = Agent(
                config, SECRET, store, MagicMock(spec=ssl.SSLContext), shutdown
            )
            agent_task = asyncio.create_task(agent.run())

            await asyncio.wait_for(server_done.wait(), timeout=5.0)
            shutdown.set()
            await asyncio.wait_for(agent_task, timeout=5.0)

    store.close()

    assert stray_results == []
    mock_exec.assert_not_called()


@pytest.mark.asyncio
async def test_nonce_replay_rejected(tmp_path: Path, free_port: int) -> None:
    """First command succeeds; replayed nonce is silently dropped."""
    config = build_config(tmp_path, free_port)
    store = NonceStore(tmp_path / "nonces.db")
    shutdown = asyncio.Event()
    server_done = asyncio.Event()

    first_results: list[dict[str, Any]] = []
    replay_results: list[dict[str, Any]] = []

    fixed_nonce = generate_nonce()

    async def handler(ws: ServerConnection) -> None:
        await _wait_for_register(ws)

        msg = sign_command_request("git_pull", nonce=fixed_nonce)

        # First send - should succeed
        await ws.send(msg)
        first_results.extend(await _collect_results(ws, count=1, timeout=2.0))

        # Replay - should be silently rejected
        await ws.send(msg)
        replay_results.extend(await _drain_non_results(ws, timeout=0.5))

        server_done.set()

    mock_exec = MagicMock(side_effect=_exec_side_effect_factory())

    with (
        patch("stormpulse.agent.reconnect.connect", side_effect=_plain_connect),
        patch("stormpulse.agent.dispatch.execute_command", mock_exec),
        patch("stormpulse.agent.loops.collect_metrics", return_value=FAKE_METRICS),
    ):
        async with serve(handler, "localhost", free_port):
            agent = Agent(
                config, SECRET, store, MagicMock(spec=ssl.SSLContext), shutdown
            )
            agent_task = asyncio.create_task(agent.run())

            await asyncio.wait_for(server_done.wait(), timeout=5.0)
            shutdown.set()
            await asyncio.wait_for(agent_task, timeout=5.0)

    store.close()

    assert len(first_results) == 1
    assert first_results[0]["payload"]["success"] is True
    assert replay_results == []
    assert mock_exec.call_count == 1


@pytest.mark.asyncio
async def test_stale_timestamp_rejected(tmp_path: Path, free_port: int) -> None:
    """Command with timestamp 5 minutes in the past is silently dropped."""
    config = build_config(tmp_path, free_port)
    store = NonceStore(tmp_path / "nonces.db")
    shutdown = asyncio.Event()
    server_done = asyncio.Event()
    stray_results: list[dict[str, Any]] = []

    async def handler(ws: ServerConnection) -> None:
        await _wait_for_register(ws)

        stale_ts = datetime.now(UTC) - timedelta(seconds=300)
        await ws.send(sign_command_request("git_pull", ts=stale_ts))

        stray_results.extend(await _drain_non_results(ws, timeout=0.5))
        server_done.set()

    mock_exec = MagicMock()

    with (
        patch("stormpulse.agent.reconnect.connect", side_effect=_plain_connect),
        patch("stormpulse.agent.dispatch.execute_command", mock_exec),
        patch("stormpulse.agent.loops.collect_metrics", return_value=FAKE_METRICS),
    ):
        async with serve(handler, "localhost", free_port):
            agent = Agent(
                config, SECRET, store, MagicMock(spec=ssl.SSLContext), shutdown
            )
            agent_task = asyncio.create_task(agent.run())

            await asyncio.wait_for(server_done.wait(), timeout=5.0)
            shutdown.set()
            await asyncio.wait_for(agent_task, timeout=5.0)

    store.close()

    assert stray_results == []
    mock_exec.assert_not_called()


@pytest.mark.asyncio
async def test_sequence_stop_on_failure(tmp_path: Path, free_port: int) -> None:
    """3-command sequence where 2nd fails - only 2 results sent."""
    config = build_config(tmp_path, free_port)
    store = NonceStore(tmp_path / "nonces.db")
    shutdown = asyncio.Event()
    server_done = asyncio.Event()
    results: list[dict[str, Any]] = []

    async def handler(ws: ServerConnection) -> None:
        await _wait_for_register(ws)

        msg = sign_command_sequence(
            ["git_pull", "docker_logs"],
            stop_on_failure=True,
        )
        await ws.send(msg)

        # Collect results - expect exactly 1 (2nd skipped due to 1st failure)
        results.extend(await _collect_results(ws, count=2, timeout=3.0))
        server_done.set()

    mock_exec = MagicMock(
        side_effect=_exec_side_effect_factory(fail_commands={"git_pull"}),
    )

    with (
        patch("stormpulse.agent.reconnect.connect", side_effect=_plain_connect),
        patch("stormpulse.agent.dispatch.execute_command", mock_exec),
        patch("stormpulse.agent.loops.collect_metrics", return_value=FAKE_METRICS),
    ):
        async with serve(handler, "localhost", free_port):
            agent = Agent(
                config, SECRET, store, MagicMock(spec=ssl.SSLContext), shutdown
            )
            agent_task = asyncio.create_task(agent.run())

            await asyncio.wait_for(server_done.wait(), timeout=5.0)
            shutdown.set()
            await asyncio.wait_for(agent_task, timeout=5.0)

    store.close()

    assert len(results) == 1
    assert results[0]["payload"]["command"] == "git_pull"
    assert results[0]["payload"]["success"] is False

    assert mock_exec.call_count == 1


@pytest.mark.asyncio
async def test_sequence_all_succeed(tmp_path: Path, free_port: int) -> None:
    """2-command sequence where all succeed - 2 results in order."""
    config = build_config(tmp_path, free_port)
    store = NonceStore(tmp_path / "nonces.db")
    shutdown = asyncio.Event()
    server_done = asyncio.Event()
    results: list[dict[str, Any]] = []

    async def handler(ws: ServerConnection) -> None:
        await _wait_for_register(ws)

        msg = sign_command_sequence(
            ["git_pull", "docker_logs"],
            stop_on_failure=True,
        )
        await ws.send(msg)

        results.extend(await _collect_results(ws, count=2, timeout=3.0))
        server_done.set()

    with (
        patch("stormpulse.agent.reconnect.connect", side_effect=_plain_connect),
        patch(
            "stormpulse.agent.dispatch.execute_command",
            side_effect=_exec_side_effect_factory(),
        ),
        patch("stormpulse.agent.loops.collect_metrics", return_value=FAKE_METRICS),
    ):
        async with serve(handler, "localhost", free_port):
            agent = Agent(
                config, SECRET, store, MagicMock(spec=ssl.SSLContext), shutdown
            )
            agent_task = asyncio.create_task(agent.run())

            await asyncio.wait_for(server_done.wait(), timeout=5.0)
            shutdown.set()
            await asyncio.wait_for(agent_task, timeout=5.0)

    store.close()

    assert len(results) == 2
    commands = [r["payload"]["command"] for r in results]
    assert commands == ["git_pull", "docker_logs"]
    assert all(r["payload"]["success"] is True for r in results)

    # All share the same sequence_id
    seq_ids = {r["payload"]["sequence_id"] for r in results}
    assert len(seq_ids) == 1
    assert None not in seq_ids


@pytest.mark.asyncio
async def test_sequence_unknown_command(tmp_path: Path, free_port: int) -> None:
    """Sequence with a bogus command name - pre-validation blocks all execution."""
    config = build_config(tmp_path, free_port)
    store = NonceStore(tmp_path / "nonces.db")
    shutdown = asyncio.Event()
    server_done = asyncio.Event()
    stray_results: list[dict[str, Any]] = []

    async def handler(ws: ServerConnection) -> None:
        await _wait_for_register(ws)

        msg = sign_command_sequence(
            ["git_pull", "totally_bogus_command"],
        )
        await ws.send(msg)

        stray_results.extend(await _drain_non_results(ws, timeout=0.5))
        server_done.set()

    mock_exec = MagicMock()

    with (
        patch("stormpulse.agent.reconnect.connect", side_effect=_plain_connect),
        patch("stormpulse.agent.dispatch.execute_command", mock_exec),
        patch("stormpulse.agent.loops.collect_metrics", return_value=FAKE_METRICS),
    ):
        async with serve(handler, "localhost", free_port):
            agent = Agent(
                config, SECRET, store, MagicMock(spec=ssl.SSLContext), shutdown
            )
            agent_task = asyncio.create_task(agent.run())

            await asyncio.wait_for(server_done.wait(), timeout=5.0)
            shutdown.set()
            await asyncio.wait_for(agent_task, timeout=5.0)

    store.close()

    assert stray_results == []
    mock_exec.assert_not_called()


@pytest.mark.asyncio
async def test_heartbeat_and_metrics_flow(tmp_path: Path, free_port: int) -> None:
    """After register, agent sends heartbeats and metrics over the real socket."""
    config = build_config(tmp_path, free_port)
    store = NonceStore(tmp_path / "nonces.db")
    shutdown = asyncio.Event()
    server_done = asyncio.Event()

    heartbeats: list[dict[str, Any]] = []
    metrics_msgs: list[dict[str, Any]] = []

    async def handler(ws: ServerConnection) -> None:
        await _wait_for_register(ws)

        # Collect messages for ~0.3s
        deadline = asyncio.get_event_loop().time() + 0.3
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except (TimeoutError, Exception):
                break
            data = json.loads(raw)
            if data["type"] == "heartbeat":
                heartbeats.append(data)
            elif data["type"] == "metrics.push":
                metrics_msgs.append(data)

        server_done.set()

    with (
        patch("stormpulse.agent.reconnect.connect", side_effect=_plain_connect),
        patch("stormpulse.agent.loops.collect_metrics", return_value=FAKE_METRICS),
    ):
        async with serve(handler, "localhost", free_port):
            agent = Agent(
                config, SECRET, store, MagicMock(spec=ssl.SSLContext), shutdown
            )
            agent_task = asyncio.create_task(agent.run())

            await asyncio.wait_for(server_done.wait(), timeout=5.0)
            shutdown.set()
            await asyncio.wait_for(agent_task, timeout=5.0)

    store.close()

    assert len(heartbeats) >= 2
    assert len(metrics_msgs) >= 2

    for hb in heartbeats:
        assert hb["agent_id"] == AGENT_ID
        assert hb["payload"] == {}

    for m in metrics_msgs:
        assert m["agent_id"] == AGENT_ID
        assert "cpu_percent" in m["payload"]


@pytest.mark.asyncio
async def test_reconnect_after_disconnect(tmp_path: Path, free_port: int) -> None:
    """Server closes WebSocket; agent reconnects and sends register again."""
    config = build_config(tmp_path, free_port)
    store = NonceStore(tmp_path / "nonces.db")
    shutdown = asyncio.Event()

    connection_count = 0
    registers: list[dict[str, Any]] = []
    second_register_received = asyncio.Event()

    async def handler(ws: ServerConnection) -> None:
        nonlocal connection_count
        connection_count += 1

        try:
            reg = await _wait_for_register(ws, timeout=2.0)
            registers.append(reg)
        except TimeoutError:
            return

        if connection_count == 1:
            await ws.close()
        elif connection_count >= 2:
            second_register_received.set()
            # Keep connection alive until shutdown
            try:
                while not shutdown.is_set():
                    await asyncio.sleep(0.05)
            except Exception:
                pass

    with (
        patch("stormpulse.agent.reconnect.connect", side_effect=_plain_connect),
        patch("stormpulse.agent.loops.collect_metrics", return_value=FAKE_METRICS),
    ):
        async with serve(handler, "localhost", free_port):
            agent = Agent(
                config, SECRET, store, MagicMock(spec=ssl.SSLContext), shutdown
            )
            agent_task = asyncio.create_task(agent.run())

            await asyncio.wait_for(second_register_received.wait(), timeout=5.0)
            shutdown.set()
            await asyncio.wait_for(agent_task, timeout=5.0)

    store.close()

    assert connection_count >= 2
    assert len(registers) >= 2
    for reg in registers:
        assert reg["agent_id"] == AGENT_ID
        assert reg["payload"]["pulse_token"] == PULSE_TOKEN


@pytest.mark.asyncio
async def test_shutdown_during_backoff(tmp_path: Path, free_port: int) -> None:
    """Shutdown event during reconnect backoff - agent exits without hanging."""
    config = build_config(tmp_path, free_port)
    store = NonceStore(tmp_path / "nonces.db")
    shutdown = asyncio.Event()
    connected = asyncio.Event()

    async def handler(ws: ServerConnection) -> None:
        try:
            await _wait_for_register(ws, timeout=1.0)
        except TimeoutError:
            pass
        connected.set()
        await ws.close()

    with (
        patch("stormpulse.agent.reconnect.connect", side_effect=_plain_connect),
        patch("stormpulse.agent.loops.collect_metrics", return_value=FAKE_METRICS),
    ):
        async with serve(handler, "localhost", free_port):
            agent = Agent(
                config, SECRET, store, MagicMock(spec=ssl.SSLContext), shutdown
            )
            agent_task = asyncio.create_task(agent.run())

            # Wait for first connection-then-disconnect cycle
            await asyncio.wait_for(connected.wait(), timeout=3.0)

            # Agent is now in backoff. Set shutdown after a short delay.
            await asyncio.sleep(0.05)
            shutdown.set()

            # Agent should exit promptly
            await asyncio.wait_for(agent_task, timeout=3.0)

    store.close()
