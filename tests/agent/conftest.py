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
from tests.helpers import SECRET, build_config, build_garage_config


@pytest.fixture
def agent_with_garage(
    tmp_path: Path,
    nonce_store: NonceStore,
    ssl_ctx: MagicMock,
    shutdown: asyncio.Event,
) -> Callable[..., Agent]:
    """Factory: build an Agent whose Config has [garage] populated.

    Use ``agent_with_garage()`` for an enabled garage, or
    ``agent_with_garage(enabled=False)`` for the disabled-but-present case.
    """

    def _build(*, enabled: bool = True) -> Agent:
        garage = build_garage_config(tmp_path)
        if not enabled:
            garage = replace(garage, enabled=False)
        cfg = build_config(tmp_path, garage=garage)
        return Agent(cfg, SECRET, nonce_store, ssl_ctx, shutdown)

    return _build
