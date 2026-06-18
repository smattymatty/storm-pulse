"""Tests for the periodic loop bodies."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from stormpulse.agent import Agent, loops
from stormpulse.protocol import MetricsPayload
from tests.helpers import FAKE_METRICS, get_garage_state, make_fake_garage_state

# ---------------------------------------------------------------------------
# Heartbeat loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_loop_sends_messages(
    agent: Agent, shutdown: asyncio.Event
) -> None:
    ws = AsyncMock()
    sent: list[str] = []
    ws.send = AsyncMock(side_effect=lambda msg: sent.append(msg))

    async def stop_after_delay() -> None:
        await asyncio.sleep(0.15)
        shutdown.set()

    await asyncio.gather(
        loops.heartbeat_loop(agent, ws),
        stop_after_delay(),
    )
    assert len(sent) >= 2
    for msg in sent:
        data = json.loads(msg)
        assert data["type"] == "heartbeat"


@pytest.mark.asyncio
async def test_heartbeat_loop_stops_on_shutdown(
    agent: Agent, shutdown: asyncio.Event
) -> None:
    ws = AsyncMock()
    shutdown.set()
    await loops.heartbeat_loop(agent, ws)
    ws.send.assert_not_called()


# ---------------------------------------------------------------------------
# Metrics loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("stormpulse.agent.loops.collect_metrics")
async def test_metrics_loop_sends_metrics(
    mock_collect: MagicMock,
    agent: Agent,
    shutdown: asyncio.Event,
) -> None:
    mock_collect.return_value = FAKE_METRICS
    ws = AsyncMock()
    sent: list[str] = []
    ws.send = AsyncMock(side_effect=lambda msg: sent.append(msg))

    async def stop_after_delay() -> None:
        await asyncio.sleep(0.15)
        shutdown.set()

    await asyncio.gather(
        loops.metrics_loop(agent, ws),
        stop_after_delay(),
    )
    assert len(sent) >= 2
    for msg in sent:
        data = json.loads(msg)
        assert data["type"] == "metrics.push"
    assert mock_collect.call_count >= 2


@pytest.mark.asyncio
@patch("stormpulse.agent.loops.collect_metrics")
async def test_metrics_loop_survives_collection_error(
    mock_collect: MagicMock,
    agent: Agent,
    shutdown: asyncio.Event,
) -> None:
    call_count = 0

    def side_effect(*args: object) -> MetricsPayload:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("psutil broke")
        return FAKE_METRICS

    mock_collect.side_effect = side_effect
    ws = AsyncMock()

    async def stop_after_delay() -> None:
        await asyncio.sleep(0.15)
        shutdown.set()

    await asyncio.gather(
        loops.metrics_loop(agent, ws),
        stop_after_delay(),
    )
    # Should have continued past the error and sent at least one metrics push
    assert ws.send.call_count >= 1


# ---------------------------------------------------------------------------
# Integration state loop (disabled case gated at reconnect - see TestGarageLiveGate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("stormpulse.garage.state.collect_garage_state")
async def test_integration_state_loop_updates_state(
    mock_collect: MagicMock,
    agent_with_garage: Callable[..., Agent],
    shutdown: asyncio.Event,
) -> None:
    fake = make_fake_garage_state()
    mock_collect.return_value = fake
    ag = agent_with_garage()
    ws = AsyncMock()

    async def stop_after_delay() -> None:
        await asyncio.sleep(0.12)
        shutdown.set()

    await asyncio.gather(
        loops.integration_state_loop(ag, ws, "garage"), stop_after_delay()
    )
    assert get_garage_state(ag) is fake
    assert mock_collect.call_count >= 1
