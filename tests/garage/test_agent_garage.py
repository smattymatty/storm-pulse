"""Tests for garage-related agent behavior (CORE-005 generic integration runtime)."""

from __future__ import annotations

import asyncio
import ssl
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from stormpulse.agent import Agent, loops, refresh
from stormpulse.auth import NonceStore
from stormpulse.config import Config
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.state import GarageState, GarageStateReader
from stormpulse.signoff import SignoffState
from tests.helpers import build_config

SECRET = b"test-secret-key-256-bits-long!!!"


def _make_config(
    tmp_path: Path,
    garage: GarageConfig | None = None,
    *,
    metrics_push_interval: float = 0.05,
) -> Config:
    return build_config(
        tmp_path, garage=garage, metrics_push_interval=metrics_push_interval
    )


def _garage_cfg(tmp_path: Path, *, enabled: bool = True) -> GarageConfig:
    return GarageConfig(
        enabled=enabled,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=tmp_path / "garage.toml",
    )


def _make_agent(config: Config, tmp_path: Path) -> tuple[Agent, asyncio.Event]:
    nonce_store = NonceStore(tmp_path / "nonces.db")
    shutdown = asyncio.Event()
    ssl_ctx = MagicMock(spec=ssl.SSLContext)
    agent = Agent(
        config,
        SECRET,
        nonce_store,
        ssl_ctx,
        shutdown,
        signoff_state=SignoffState(config.storage.db_path.parent),
    )
    return agent, shutdown


def _garage_state(agent: Agent) -> GarageState | None:
    rt = agent.integrations.get("garage")
    state = rt.state if rt is not None else None
    return state if isinstance(state, GarageState) else None


class TestGarageLiveGate:
    """Bootstrap resolves a per-Integration runtime with a status the wire reports."""

    def test_garage_absent_without_config(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, garage=None)
        agent, _ = _make_agent(config, tmp_path)
        assert "garage" not in agent.integrations

    def test_garage_disabled_choice_when_disabled(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, garage=_garage_cfg(tmp_path, enabled=False))
        agent, _ = _make_agent(config, tmp_path)
        assert agent.integrations["garage"].status == "disabled_choice"

    def test_garage_live_when_enabled_and_preconditions_pass(
        self, tmp_path: Path
    ) -> None:
        # Preconditions are stubbed to pass by the autouse fixture in
        # tests/conftest.py.
        config = _make_config(tmp_path, garage=_garage_cfg(tmp_path))
        agent, _ = _make_agent(config, tmp_path)
        assert agent.integrations["garage"].status == "live"


class TestGarageLoopEnabled:
    """The generic state loop updates the garage runtime when enabled."""

    @pytest.mark.asyncio
    async def test_updates_garage_state(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, garage=_garage_cfg(tmp_path))
        agent, shutdown = _make_agent(config, tmp_path)
        ws = AsyncMock()

        fake_state = GarageState(
            node_id="abc123",
            hostname="test",
            zone="zone-1",
            capacity_gb=10.0,
            data_avail_gb=8.0,
            version="v2.2.0",
            healthy=True,
            object_count=5,
            buckets=[],
            keys=[],
            peers=[],
        )

        async def run_loop() -> None:
            with patch.object(
                GarageStateReader, "collect", return_value=fake_state,
            ):
                task = asyncio.create_task(
                    loops.integration_state_loop(agent, ws, "garage")
                )
                await asyncio.sleep(0.15)
                shutdown.set()
                await task

        await run_loop()
        state = _garage_state(agent)
        assert state is not None
        assert state.node_id == "abc123"

    @pytest.mark.asyncio
    async def test_collects_before_first_wait(self, tmp_path: Path) -> None:
        """First action in the loop must be collect, not sleep."""
        # Long push interval so the loop's first sleep is effectively forever:
        # this proves collect runs BEFORE the first wait, not that it eventually
        # runs. The state loop now rides the metrics-push cadence (no per-garage
        # interval knob), so the interval lives on the metrics config.
        config = _make_config(
            tmp_path, garage=_garage_cfg(tmp_path), metrics_push_interval=600
        )
        agent, shutdown = _make_agent(config, tmp_path)
        ws = AsyncMock()

        fake_state = GarageState(
            node_id="immediate",
            hostname="test",
            zone="zone-1",
            capacity_gb=10.0,
            data_avail_gb=8.0,
            version="v2.2.0",
            healthy=True,
            object_count=5,
            buckets=[],
            keys=[],
            peers=[],
        )

        async def run_loop() -> None:
            with patch.object(
                GarageStateReader, "collect", return_value=fake_state,
            ) as mock_collect:
                task = asyncio.create_task(
                    loops.integration_state_loop(agent, ws, "garage")
                )
                # Give just enough time for the collect to run, but nowhere
                # near 600s - proves collect happens before the wait
                await asyncio.sleep(0.1)
                assert mock_collect.called, (
                    "reader.collect was not called before wait"
                )
                shutdown.set()
                await task

        await run_loop()
        state = _garage_state(agent)
        assert state is not None
        assert state.node_id == "immediate"


class TestGarageCommandsInRegistry:
    """Garage commands are merged into the registry when enabled."""

    def test_garage_commands_registered(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, garage=_garage_cfg(tmp_path))
        agent, _ = _make_agent(config, tmp_path)
        assert "garage_status" in agent.registry
        assert "garage_bucket_list" in agent.registry
        assert "garage_key_create" in agent.registry

    def test_no_garage_commands_without_config(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, garage=None)
        agent, _ = _make_agent(config, tmp_path)
        assert "garage_status" not in agent.registry
        # Original commands still present
        assert "git_pull" in agent.registry


class TestGarageRefresh:
    """garage_refresh collects state and returns result."""

    @pytest.mark.asyncio
    async def test_refresh_updates_state(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, garage=_garage_cfg(tmp_path))
        agent, _ = _make_agent(config, tmp_path)

        fake_state = GarageState(
            node_id="abc123",
            hostname="test",
            zone="zone-1",
            capacity_gb=10.0,
            data_avail_gb=8.0,
            version="v2.2.0",
            healthy=True,
            object_count=5,
            buckets=[],
            keys=[],
            peers=[],
        )
        with patch.object(
            GarageStateReader, "collect", return_value=fake_state,
        ):
            result = await refresh.collect_refresh_result(agent, "garage_refresh", "req-1", "garage")

        assert result.success is True
        assert result.command == "garage_refresh"
        assert "0 buckets" in result.stdout
        state = _garage_state(agent)
        assert state is not None
        assert state.node_id == "abc123"

    @pytest.mark.asyncio
    async def test_refresh_not_configured(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, garage=None)
        agent, _ = _make_agent(config, tmp_path)

        result = await refresh.collect_refresh_result(agent, "garage_refresh", "req-1", "garage")

        assert result.success is False
        assert result.failure_reason == "not_configured"

    @pytest.mark.asyncio
    async def test_refresh_collection_fails(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, garage=_garage_cfg(tmp_path))
        agent, _ = _make_agent(config, tmp_path)

        with patch.object(
            GarageStateReader, "collect", return_value=None,
        ):
            result = await refresh.collect_refresh_result(agent, "garage_refresh", "req-1", "garage")

        assert result.success is False
        assert result.failure_reason == "collection_failed"
        assert _garage_state(agent) is None

    def test_garage_refresh_in_registry(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, garage=_garage_cfg(tmp_path))
        agent, _ = _make_agent(config, tmp_path)
        assert "garage_refresh" in agent.registry


class TestSensitiveOutputLogging:
    """Verify sensitive_output flag prevents logging."""

    def test_sensitive_command_no_debug_log(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, garage=_garage_cfg(tmp_path))
        agent, _ = _make_agent(config, tmp_path)
        cmd_def = agent.registry["garage_key_create"]
        assert cmd_def.sensitive_output is True
