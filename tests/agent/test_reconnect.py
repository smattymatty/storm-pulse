"""Tests for the outer reconnect loop in ``Agent.run``."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from websockets.exceptions import ConnectionClosed

from stormpulse.agent import Agent


@pytest.mark.asyncio
async def test_run_reconnects_on_connection_error(
    agent: Agent,
    shutdown: asyncio.Event,
) -> None:
    attempts = 0

    def mock_connect(*args: object, **kwargs: object) -> MagicMock:
        nonlocal attempts
        attempts += 1
        if attempts >= 3:
            shutdown.set()
        raise OSError("Connection refused")

    with patch("stormpulse.agent.reconnect.connect", side_effect=mock_connect):
        await agent.run()

    assert attempts >= 2


@pytest.mark.asyncio
async def test_run_exits_on_shutdown(
    agent: Agent,
    shutdown: asyncio.Event,
) -> None:
    shutdown.set()
    await agent.run()
    # Should exit immediately without attempting to connect.


@pytest.mark.asyncio
async def test_run_reconnects_on_connection_closed(
    agent: Agent,
    shutdown: asyncio.Event,
) -> None:
    attempts = 0

    def mock_connect(*a: object, **kw: object) -> MagicMock:
        nonlocal attempts
        attempts += 1
        if attempts >= 2:
            shutdown.set()
        raise ConnectionClosed(None, None)

    with patch("stormpulse.agent.reconnect.connect", side_effect=mock_connect):
        await agent.run()
    assert attempts >= 2


@pytest.mark.asyncio
async def test_run_handles_unexpected_exception(
    agent: Agent,
    shutdown: asyncio.Event,
) -> None:
    attempts = 0

    def mock_connect(*a: object, **kw: object) -> MagicMock:
        nonlocal attempts
        attempts += 1
        if attempts >= 1:
            shutdown.set()
        raise RuntimeError("totally unexpected")

    with patch("stormpulse.agent.reconnect.connect", side_effect=mock_connect):
        await agent.run()
    assert attempts >= 1


@pytest.mark.asyncio
async def test_backoff_resets_after_stable_session(
    agent: Agent,
    shutdown: asyncio.Event,
) -> None:
    """A drop after a stable session retries at reconnect_min_seconds.

    Real sessions always end via ConnectionClosed, so the clean-return
    reset never ran: attempts and delay accumulated forever and every
    blip paid the fully-clamped delay ("attempt 51, 445246s since first
    failure" - the 2026-07-09 deploy-blackout incident). A session that
    outlived reconnect_max_seconds starts a new failure era; an
    instantly-dying session must still back off (no hot loop).
    """
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    clock = [0.0]
    # Session lifetimes, in seconds: short, stable (>= max 0.2), short.
    durations = [0.01, 10.0, 0.01]
    recorded: list[float] = []

    class _FakeConn:
        def __init__(self, *a: object, **kw: object) -> None:
            pass

        async def __aenter__(self) -> MagicMock:
            return MagicMock()

        async def __aexit__(self, *exc: object) -> bool:
            return False

    async def fake_session(ag: object, ws: object, url: str) -> None:
        clock[0] += durations.pop(0)
        raise ConnectionClosed(None, None)

    async def fake_sleep(ag: object, delay: float) -> float:
        recorded.append(delay)
        if len(recorded) >= 3:
            shutdown.set()
        return min(delay * 1.5, 0.2)

    with (
        patch("stormpulse.agent.reconnect.connect", _FakeConn),
        patch("stormpulse.agent.reconnect._run_session", fake_session),
        patch("stormpulse.agent.reconnect._sleep_with_jitter", fake_sleep),
        patch(
            "stormpulse.agent.reconnect.time",
            SimpleNamespace(monotonic=lambda: clock[0]),
        ),
    ):
        await agent.run()

    # Short session backs off (0.05 -> 0.075); the stable session resets
    # the era, so its drop retries at min again instead of 0.075.
    assert recorded == pytest.approx([0.05, 0.05, 0.075])


# ---------------------------------------------------------------------------
# log_enricher_provider (CORE-005 decision 13: enrichment keyed by parser)
# ---------------------------------------------------------------------------


def test_log_enricher_provider_is_tick_fresh(
    agent_with_garage,
) -> None:
    """The provider rebuilds from the declarer's CURRENT state on every call."""
    from stormpulse.agent.reconnect import log_enricher_provider
    from tests.helpers import make_fake_garage_state, make_garage_bucket, set_garage_state

    ag = agent_with_garage()
    provider = log_enricher_provider(ag, "garage_s3")

    bucket_id = "c" * 64
    state = make_fake_garage_state().with_items(
        [make_garage_bucket(bucket_id, alias="my-bucket")]
    )
    set_garage_state(ag, state)
    enricher = provider()
    assert enricher is not None
    assert enricher("GKkey", "my-bucket") == bucket_id

    # State changes; the NEXT provider() call must see it, not a frozen snapshot.
    set_garage_state(ag, None)
    enricher = provider()
    assert enricher is not None
    assert enricher("GKkey", "my-bucket") == ""


def test_log_enricher_provider_none_for_undeclared_parser(
    agent_with_garage,
) -> None:
    """A parser no Integration declares yields None: the shipper skips stamping."""
    from stormpulse.agent.reconnect import log_enricher_provider

    ag = agent_with_garage()
    assert log_enricher_provider(ag, "raw")() is None
