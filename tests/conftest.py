"""Shared pytest fixtures."""

from __future__ import annotations

import asyncio
import socket
import ssl
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from stormpulse.agent import Agent
from stormpulse.auth import NonceStore
from stormpulse.config import Config

from tests.helpers import SECRET, build_config


@pytest.fixture
def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def nonce_store(tmp_path: Path) -> Generator[NonceStore, None, None]:
    store = NonceStore(tmp_path / "nonces.db")
    yield store
    store.close()


@pytest.fixture
def shutdown() -> asyncio.Event:
    return asyncio.Event()


@pytest.fixture
def ssl_ctx() -> MagicMock:
    """A mock SSLContext suitable for tests that don't actually connect."""
    return MagicMock(spec=ssl.SSLContext)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Default Config for unit tests — no garage, no real port."""
    return build_config(tmp_path)


@pytest.fixture
def agent(
    config: Config,
    nonce_store: NonceStore,
    ssl_ctx: MagicMock,
    shutdown: asyncio.Event,
) -> Agent:
    return Agent(config, SECRET, nonce_store, ssl_ctx, shutdown)
