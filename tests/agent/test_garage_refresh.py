"""Tests for the generic on-demand refresh path, with garage as the reference case.

``garage_refresh`` is a ``mode="refresh"`` command synthesized for any
integration that declares ``collect_state``. The dispatcher routes it to the
generic ``stormpulse.agent.refresh`` routine, which collects a fresh snapshot
inline and triggers an immediate metrics push so the dashboard sees the
refreshed counts in the same tick.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from stormpulse.agent import Agent, dispatch, refresh
from stormpulse.garage.state import GarageStateReader
from tests.helpers import (
    FAKE_METRICS,
    get_garage_state,
    make_fake_garage_state,
    sign_command_request,
)


@pytest.mark.asyncio
@patch("stormpulse.agent.integrations_runtime.collect_metrics")
@patch.object(GarageStateReader, "collect")
async def test_garage_refresh_command_success(
    mock_collect: MagicMock,
    mock_metrics: MagicMock,
    agent_with_garage: Callable[..., Agent],
) -> None:
    fake_state = make_fake_garage_state()
    mock_collect.return_value = fake_state
    mock_metrics.return_value = FAKE_METRICS
    ag = agent_with_garage()
    ws = AsyncMock()

    await dispatch.dispatch_message(
        ag, ws, sign_command_request(command="garage_refresh")
    )

    # First send = command.result, second send = immediate metrics push
    assert ws.send.call_count == 2
    result_env = json.loads(ws.send.call_args_list[0][0][0])
    metrics_env = json.loads(ws.send.call_args_list[1][0][0])
    assert result_env["type"] == "command.result"
    assert result_env["payload"]["success"] is True
    assert metrics_env["type"] == "metrics.push"
    assert get_garage_state(ag) is fake_state


@pytest.mark.asyncio
async def test_garage_refresh_when_disabled_returns_failure(
    agent_with_garage: Callable[..., Agent],
) -> None:
    ag = agent_with_garage(enabled=False)
    result = await refresh.collect_refresh_result(ag, "garage_refresh", "req-1", "garage")
    assert result.success is False
    assert result.failure_reason == "not_configured"


@pytest.mark.asyncio
@patch.object(GarageStateReader, "collect", return_value=None)
async def test_garage_refresh_collection_failure(
    _mock: MagicMock,
    agent_with_garage: Callable[..., Agent],
) -> None:
    ag = agent_with_garage()
    result = await refresh.collect_refresh_result(ag, "garage_refresh", "req-1", "garage")
    assert result.success is False
    assert result.failure_reason == "collection_failed"
