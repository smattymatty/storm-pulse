"""The renew loop lives as long as the agent, beside reconnect (CORE-010 decision 1)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from websockets.exceptions import ConnectionClosed

from stormpulse.agent import Agent
from stormpulse.agent.cert_renew import CHECK_INTERVAL_SECONDS, cert_renew_loop
from tests.agent.test_cert_renew import URLOPEN, _cert_events, _seed, _Server


@asynccontextmanager
async def _conn(*a: object, **kw: object) -> AsyncIterator[MagicMock]:
    yield MagicMock(send=AsyncMock(), close=AsyncMock())


@pytest.mark.asyncio
async def test_loop_checks_once_a_day(agent: Agent, shutdown: asyncio.Event) -> None:
    _seed(agent, 29)
    server = _Server()
    slept: list[float] = []

    async def fake_sleep(ev: asyncio.Event, interval: float) -> bool:
        slept.append(interval)
        return len(slept) >= 2

    with (
        patch(URLOPEN, side_effect=server),
        patch("stormpulse.agent.cert_renew.sleep_or_shutdown", fake_sleep),
    ):
        await cert_renew_loop(agent)
    assert slept == [CHECK_INTERVAL_SECONDS, CHECK_INTERVAL_SECONDS]
    assert CHECK_INTERVAL_SECONDS == 86_400
    assert server.calls == 2


@pytest.mark.asyncio
async def test_loop_survives_an_unexpected_crash(agent: Agent) -> None:
    slept: list[float] = []

    async def fake_sleep(ev: asyncio.Event, interval: float) -> bool:
        slept.append(interval)
        return len(slept) >= 2

    with (
        patch(
            "stormpulse.agent.cert_renew.check_cert", side_effect=RuntimeError("bug")
        ),
        patch("stormpulse.agent.cert_renew.sleep_or_shutdown", fake_sleep),
    ):
        await cert_renew_loop(agent)
    assert len(slept) == 2
    assert [e["reason"] for e in _cert_events()] == ["unspecified", "unspecified"]


@pytest.mark.asyncio
async def test_reconnect_churn_does_not_add_attempts(
    agent: Agent, shutdown: asyncio.Event
) -> None:
    _seed(agent, 29)
    server = _Server()
    connects = 0

    def flap(*a: object, **kw: object) -> MagicMock:
        nonlocal connects
        connects += 1
        if connects >= 6:
            shutdown.set()
        raise OSError("Connection refused")

    with (
        patch(URLOPEN, side_effect=server),
        patch("stormpulse.agent.reconnect.connect", side_effect=flap),
    ):
        await agent.run()
    assert connects >= 6
    assert server.calls == 1


@pytest.mark.asyncio
async def test_session_teardowns_do_not_add_attempts(
    agent: Agent, shutdown: asyncio.Event
) -> None:
    _seed(agent, 29)
    server = _Server()
    sessions = 0

    async def drop(ag: object, ws: object) -> None:
        nonlocal sessions
        sessions += 1
        await asyncio.sleep(0.01)
        if sessions >= 5:
            shutdown.set()
        raise ConnectionClosed(None, None)

    with (
        patch(URLOPEN, side_effect=server),
        patch("stormpulse.agent.reconnect.connect", _conn),
        patch("stormpulse.agent.reconnect.send_register", AsyncMock()),
        patch("stormpulse.agent.reconnect.dispatch.receive_loop", drop),
    ):
        await agent.run()
    assert sessions >= 5
    assert server.calls == 1


@pytest.mark.asyncio
async def test_any_renewal_bug_leaves_the_connection_loop_running(
    agent: Agent, shutdown: asyncio.Event
) -> None:
    """A non-RuntimeError escaping the loop would cancel reconnect via the TaskGroup."""
    connects = 0

    def flap(*a: object, **kw: object) -> MagicMock:
        nonlocal connects
        connects += 1
        if connects >= 4:
            shutdown.set()
        raise OSError("Connection refused")

    with (
        patch("stormpulse.agent.cert_renew.check_cert", side_effect=KeyError("bug")),
        patch("stormpulse.agent.reconnect.connect", side_effect=flap),
    ):
        await asyncio.wait_for(agent.run(), timeout=30)
    assert connects >= 4
    assert [e["reason"] for e in _cert_events()] == ["unspecified"]
