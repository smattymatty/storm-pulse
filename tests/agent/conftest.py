"""Fixtures shared by tests in ``tests/agent/``."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from stormpulse import events
from stormpulse.agent import Agent
from stormpulse.agent.event_spool import SpoolFeeder, spool_feeders
from stormpulse.auth import NonceStore
from stormpulse.signoff import SignoffState
from tests.agent.spool_support import declared, runtime
from tests.helpers import (
    SECRET,
    build_config,
    build_garage_config,
    matched_test_session,
)


@pytest.fixture
def agent_with_garage(
    tmp_path: Path,
    nonce_store: NonceStore,
    ssl_ctx: MagicMock,
    shutdown: asyncio.Event,
) -> Callable[..., Agent]:
    """Factory: build an Agent whose Config has [garage] populated."""

    def _build(*, enabled: bool = True) -> Agent:
        garage = build_garage_config(tmp_path)
        if not enabled:
            garage = replace(garage, enabled=False)
        cfg = build_config(tmp_path, garage=garage)
        return matched_test_session(
            Agent(
                cfg,
                SECRET,
                nonce_store,
                ssl_ctx,
                shutdown,
                signoff_state=SignoffState(cfg.storage.db_path.parent),
            )
        )

    return _build


@pytest.fixture
def event_registry() -> Iterator[None]:
    """Restore the declared integration sources a spool test adds."""
    registry, spools = dict(events._INTEGRATION_REGISTRY), dict(events._SPOOL_DIRS)
    yield
    events._INTEGRATION_REGISTRY.clear()
    events._INTEGRATION_REGISTRY.update(registry)
    events._SPOOL_DIRS.clear()
    events._SPOOL_DIRS.update(spools)


@pytest.fixture
def spool(tmp_path: Path, event_registry: None) -> Iterator[Path]:
    path = tmp_path / "spool"
    path.mkdir(mode=0o700)
    path.chmod(0o700)
    yield path
    if path.is_dir() and not path.is_symlink():
        path.chmod(0o700)  # a test may close it; let tmp cleanup in


@pytest.fixture
def feeder(spool: Path) -> SpoolFeeder:
    return spool_feeders({"probe": runtime("live", declared(spool))})["probe"]
