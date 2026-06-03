"""Fixtures shared by tests in ``tests/agent/``."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from stormpulse.agent import Agent
from stormpulse.auth import NonceStore
from stormpulse.signoff import SignoffState
from tests.helpers import SECRET, build_config, build_garage_config


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
        return Agent(
            cfg,
            SECRET,
            nonce_store,
            ssl_ctx,
            shutdown,
            signoff_state=SignoffState(cfg.storage.db_path.parent),
        )

    return _build
