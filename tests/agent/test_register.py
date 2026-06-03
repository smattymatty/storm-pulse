"""Tests for the initial register envelope sent right after a connection."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from stormpulse.agent import Agent
from stormpulse.agent.register import send_register
from stormpulse.auth import NonceStore
from stormpulse.config import Config
from stormpulse.signoff import SignoffState
from tests.helpers import SECRET, make_fake_garage_state


@pytest.mark.asyncio
async def test_send_register_sends_register_envelope(agent: Agent) -> None:
    """The first frame sent on a new connection is a register envelope.

    Carries the agent id, version, pulse token, and the resolved
    command catalogue. No garage block when [garage] isn't configured.
    """
    ws = AsyncMock()

    with patch(
        "stormpulse.agent.register.collect_system_inventory",
        return_value=None,
    ):
        await send_register(agent, ws, "wss://example.com/ws/")

    ws.send.assert_called_once()
    data = json.loads(ws.send.call_args[0][0])
    assert data["type"] == "register"
    assert data["agent_id"] == agent.config.agent.id
    payload = data["payload"]
    assert payload["pulse_token"] == agent.config.agent.pulse_token
    assert payload["version"]
    assert payload["garage"] is None
    assert "git_pull" in payload["commands"]


@pytest.mark.asyncio
async def test_send_register_includes_garage_snapshot_when_enabled(
    agent_with_garage: Callable[..., Agent],
) -> None:
    """When [garage] is enabled the register carries a discovered snapshot."""
    fake_state = make_fake_garage_state()
    ag = agent_with_garage()
    ws = AsyncMock()

    with (
        patch("stormpulse.agent.register.discover_garage", return_value=fake_state),
        patch("stormpulse.agent.register.collect_system_inventory", return_value=None),
    ):
        await send_register(ag, ws, "wss://example.com/ws/")

    data = json.loads(ws.send.call_args[0][0])
    assert data["payload"]["garage"] is not None
    assert data["payload"]["garage"]["node_id"] == fake_state.node_id
    assert ag.garage_state is fake_state


@pytest.mark.asyncio
async def test_send_register_reports_seal_state(
    config: Config,
    nonce_store: NonceStore,
    ssl_ctx: MagicMock,
    shutdown: asyncio.Event,
    tmp_path: Path,
) -> None:
    """The register frame advertises the agent's current seal state."""
    sealed_state = SignoffState(config.storage.db_path.parent)
    sealed_state.seal()
    try:
        ag = Agent(
            config,
            SECRET,
            nonce_store,
            ssl_ctx,
            shutdown,
            signoff_state=sealed_state,
        )
        ws = AsyncMock()

        with patch(
            "stormpulse.agent.register.collect_system_inventory",
            return_value=None,
        ):
            await send_register(ag, ws, "wss://example.com/ws/")

        data = json.loads(ws.send.call_args[0][0])
        assert data["payload"]["signoff_sealed"] is True
        # When sealed there is no unsealed-since timestamp to advertise.
        assert data["payload"]["unsealed_since"] is None
    finally:
        sealed_state.unseal()


@pytest.mark.asyncio
async def test_send_register_advertises_unsealed_since(
    config: Config,
    nonce_store: NonceStore,
    ssl_ctx: MagicMock,
    shutdown: asyncio.Event,
) -> None:
    """An unsealed agent puts the wall-clock unsealed_since on the register.

    The dashboard uses this to render "unsealed for X" and to fire its
    "unsealed > N hours" pager without having to guess from its own
    register-history table — the agent owns the authoritative timestamp.
    """
    from datetime import datetime

    state = SignoffState(config.storage.db_path.parent)
    state.seal()
    state.unseal()  # now has a real unsealed_since marker
    ag = Agent(
        config,
        SECRET,
        nonce_store,
        ssl_ctx,
        shutdown,
        signoff_state=state,
    )
    ws = AsyncMock()

    with patch(
        "stormpulse.agent.register.collect_system_inventory",
        return_value=None,
    ):
        await send_register(ag, ws, "wss://example.com/ws/")

    data = json.loads(ws.send.call_args[0][0])
    assert data["payload"]["signoff_sealed"] is False
    assert data["payload"]["unsealed_since"] is not None
    # Round-trips as a valid ISO timestamp.
    parsed = datetime.fromisoformat(data["payload"]["unsealed_since"])
    assert parsed.tzinfo is not None


@pytest.mark.asyncio
async def test_send_register_mirrors_to_pulse_logger(
    config: Config,
    nonce_store: NonceStore,
    ssl_ctx: MagicMock,
    shutdown: asyncio.Event,
) -> None:
    """A connect event is mirrored to the PulseLogger when one is configured."""
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
    ws = AsyncMock()

    with patch(
        "stormpulse.agent.register.collect_system_inventory",
        return_value=None,
    ):
        await send_register(ag, ws, "wss://test.example/ws/")

    pulse_logger.info.assert_called_once()
    args = pulse_logger.info.call_args
    assert args[0][0] == "Connected to dashboard"
    assert args[0][1] == "connection"
    assert args[0][2]["url"] == "wss://test.example/ws/"
