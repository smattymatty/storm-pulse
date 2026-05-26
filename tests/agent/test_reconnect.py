"""Tests for the outer reconnect loop in ``Agent.run``."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from websockets.exceptions import ConnectionClosed

from stormpulse.agent import Agent
from stormpulse.auth import NonceStore
from stormpulse.config import Config

from tests.helpers import SECRET


@pytest.mark.asyncio
async def test_run_reconnects_on_connection_error(
    config: Config,
    nonce_store: NonceStore,
    ssl_ctx: MagicMock,
    shutdown: asyncio.Event,
) -> None:
    agent = Agent(config, SECRET, nonce_store, ssl_ctx, shutdown)
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
    config: Config,
    nonce_store: NonceStore,
    ssl_ctx: MagicMock,
    shutdown: asyncio.Event,
) -> None:
    agent = Agent(config, SECRET, nonce_store, ssl_ctx, shutdown)
    shutdown.set()
    await agent.run()
    # Should exit immediately without attempting to connect.


@pytest.mark.asyncio
async def test_run_reconnects_on_connection_closed(
    config: Config,
    nonce_store: NonceStore,
    ssl_ctx: MagicMock,
    shutdown: asyncio.Event,
) -> None:
    ag = Agent(config, SECRET, nonce_store, ssl_ctx, shutdown)
    attempts = 0

    def mock_connect(*a: object, **kw: object) -> MagicMock:
        nonlocal attempts
        attempts += 1
        if attempts >= 2:
            shutdown.set()
        raise ConnectionClosed(None, None)

    with patch("stormpulse.agent.reconnect.connect", side_effect=mock_connect):
        await ag.run()
    assert attempts >= 2


@pytest.mark.asyncio
async def test_run_handles_unexpected_exception(
    config: Config,
    nonce_store: NonceStore,
    ssl_ctx: MagicMock,
    shutdown: asyncio.Event,
) -> None:
    ag = Agent(config, SECRET, nonce_store, ssl_ctx, shutdown)
    attempts = 0

    def mock_connect(*a: object, **kw: object) -> MagicMock:
        nonlocal attempts
        attempts += 1
        if attempts >= 1:
            shutdown.set()
        raise RuntimeError("totally unexpected")

    with patch("stormpulse.agent.reconnect.connect", side_effect=mock_connect):
        await ag.run()
    assert attempts >= 1
