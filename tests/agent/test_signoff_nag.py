"""Tests for the periodic unsealed-state nag loop."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from stormpulse.agent import Agent, signoff_nag
from stormpulse.auth import NonceStore
from stormpulse.config import Config
from stormpulse.signoff import SignoffState


@pytest.fixture
def fast_nag_interval() -> Generator[None, None, None]:
    """Shrink the nag interval so tests can observe ≥1 tick in <1s."""
    original = signoff_nag.NAG_INTERVAL_SECONDS
    signoff_nag.NAG_INTERVAL_SECONDS = 0.05
    try:
        yield
    finally:
        signoff_nag.NAG_INTERVAL_SECONDS = original


@pytest.mark.asyncio
async def test_nag_loop_silent_when_sealed(
    agent: Agent,
    shutdown: asyncio.Event,
    fast_nag_interval: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A sealed agent emits no warning regardless of how long the loop runs."""
    agent.signoff_state.seal()
    ws = AsyncMock()

    async def stop_after() -> None:
        await asyncio.sleep(0.15)
        shutdown.set()

    with caplog.at_level(logging.WARNING, logger="stormpulse.agent.signoff_nag"):
        await asyncio.gather(
            signoff_nag.signoff_nag_loop(agent, ws),
            stop_after(),
        )
    assert not any("UNSEALED" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_nag_loop_warns_while_unsealed(
    agent: Agent,
    shutdown: asyncio.Event,
    fast_nag_interval: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each tick while unsealed emits one warning naming the duration."""
    agent.signoff_state.seal()
    agent.signoff_state.unseal()
    ws = AsyncMock()

    async def stop_after() -> None:
        await asyncio.sleep(0.18)
        shutdown.set()

    with caplog.at_level(logging.WARNING, logger="stormpulse.agent.signoff_nag"):
        await asyncio.gather(
            signoff_nag.signoff_nag_loop(agent, ws),
            stop_after(),
        )
    warnings = [r for r in caplog.records if "UNSEALED" in r.message]
    assert len(warnings) >= 2
    assert "Reseal" in warnings[0].message


@pytest.mark.asyncio
async def test_nag_loop_mirrors_to_pulse_logger(
    config: Config,
    nonce_store: NonceStore,
    ssl_ctx: MagicMock,
    shutdown: asyncio.Event,
    fast_nag_interval: None,
    tmp_path: Path,
) -> None:
    """When a PulseLogger is configured the nag also pushes a warning event."""
    from tests.helpers import SECRET

    pulse_logger = MagicMock()
    ag = Agent(
        config,
        SECRET,
        nonce_store,
        ssl_ctx,
        shutdown,
        signoff_state=SignoffState(config.storage.db_path.parent),
        pulse_logger=pulse_logger,
    )
    ag.signoff_state.seal()
    ag.signoff_state.unseal()
    ws = AsyncMock()

    async def stop_after() -> None:
        await asyncio.sleep(0.12)
        shutdown.set()

    await asyncio.gather(
        signoff_nag.signoff_nag_loop(ag, ws),
        stop_after(),
    )
    assert pulse_logger.warning.called
    args = pulse_logger.warning.call_args
    assert args[0][0] == "Agent unsealed"
    assert args[0][1] == "signoff"
    assert "duration" in args[0][2]


@pytest.mark.asyncio
async def test_nag_loop_exits_promptly_on_shutdown(
    agent: Agent,
    shutdown: asyncio.Event,
) -> None:
    """Shutdown fired before the first wait should still terminate the loop."""
    agent.signoff_state.seal()
    shutdown.set()
    ws = AsyncMock()
    await asyncio.wait_for(signoff_nag.signoff_nag_loop(agent, ws), timeout=1.0)
