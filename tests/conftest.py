"""Shared pytest fixtures."""

from __future__ import annotations

import asyncio
import socket
import ssl
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from stormpulse.agent import Agent
from stormpulse.agent import bootstrap as agent_bootstrap
from stormpulse.auth import NonceStore
from stormpulse.config import Config
from stormpulse.signoff import SignoffState
from tests.helpers import SECRET, build_config


@pytest.fixture(autouse=True)
def _garage_preconditions_pass_in_tests() -> Generator[None, None, None]:
    """Default Garage preconditions to PASS in every test.

    Per ADR GARAGE-000, the bootstrap path runs substrate + version +
    auth checks before merging the garage command set. Those checks
    shell out to ``findmnt`` and ``docker``; they fail closed in the
    test environment, which would silently drop the garage command set
    from every Agent constructed in a test.

    Tests that explicitly want to exercise the precondition-failed
    path patch ``run_garage_preconditions`` themselves with a non-None
    return value.
    """
    with patch.object(
        agent_bootstrap,
        "run_garage_preconditions",
        return_value=None,
    ):
        yield


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
def signoff_state(config: Config) -> SignoffState:
    """Default unsealed SignoffState rooted at the test's tmp_path."""
    return SignoffState(config.storage.db_path.parent)


@pytest.fixture
def agent(
    config: Config,
    nonce_store: NonceStore,
    ssl_ctx: MagicMock,
    shutdown: asyncio.Event,
    signoff_state: SignoffState,
) -> Agent:
    return Agent(
        config, SECRET, nonce_store, ssl_ctx, shutdown, signoff_state=signoff_state
    )
