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
from stormpulse.auth import NonceStore
from stormpulse.config import Config
from stormpulse.garage import integration as garage_integration
from stormpulse.garage.state import GarageStateReader
from stormpulse.signoff import SignoffState
from tests.helpers import SECRET, build_config


@pytest.fixture(autouse=True)
def _fresh_garage_state_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give each test a fresh process-global garage state reader.

    The reader is a process-lifetime singleton by design: its topology cache
    must survive a websocket reconnect (same cluster), so a per-connection
    reader would re-read topology on every reconnect, burning admin calls during
    a reconnect storm. Process-lifetime data, modelled as a process-global.

    That makes it shared mutable state across tests. Any test that exercises the
    REAL ``collect`` through the global would otherwise inherit another test's
    call counter and cached topology, producing order-dependent failures. This
    swaps in a fresh instance per test (auto-reverted by monkeypatch), so
    isolation holds structurally - for tests in any directory, not just
    ``tests/garage/`` - rather than by the accident of tests happening to patch
    ``GarageStateReader.collect``.
    """
    monkeypatch.setattr(garage_integration, "_state_reader", GarageStateReader())


@pytest.fixture(autouse=True)
def _garage_preconditions_pass_in_tests() -> Generator[None, None, None]:
    """Default Garage preconditions to PASS in every test.

    Per ADR GARAGE-000, the bootstrap path runs substrate + version +
    auth checks before merging the garage command set. Those checks
    shell out to ``findmnt`` and ``docker``; they fail closed in the
    test environment, which would silently drop the garage command set
    from every Agent constructed in a test.

    Tests that explicitly want to exercise the precondition-failed
    path patch ``stormpulse.garage.integration.run_preconditions``
    themselves with a non-None return value. The garage Integration's
    precondition wrapper resolves this name at call time (CORE-005), so
    patching it here reaches the bootstrap path without touching the real
    orchestrator that ``preconditions``' own tests call.
    """
    with patch.object(
        garage_integration,
        "run_preconditions",
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
    """Default Config for unit tests - no garage, no real port."""
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
