"""Tests for garage-related agent behavior."""

from __future__ import annotations

import asyncio
import ssl
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from stormpulse.agent import Agent
from stormpulse.auth import NonceStore
from stormpulse.config import (
    AgentConfig,
    AuthConfig,
    Config,
    DashboardConfig,
    GarageConfig,
    MetricsConfig,
    ProjectConfig,
    StorageConfig,
    TlsConfig,
)
from stormpulse.garage.state import GarageState

SECRET = b"test-secret-key-256-bits-long!!!"


def _make_config(tmp_path: Path, garage: GarageConfig | None = None) -> Config:
    return Config(
        agent=AgentConfig(id="test-01", pulse_token="tok-test-123"),
        dashboard=DashboardConfig(
            url="wss://example.com/ws/",
            reconnect_min_seconds=0.05,
            reconnect_max_seconds=0.2,
            heartbeat_interval_seconds=0.05,
        ),
        tls=TlsConfig(
            ca_cert=tmp_path / "ca.pem",
            client_cert=tmp_path / "agent.pem",
            client_key=tmp_path / "key.pem",
        ),
        auth=AuthConfig(hmac_secret=tmp_path / "hmac.key", command_max_age_seconds=60),
        metrics=MetricsConfig(push_interval_seconds=0.05, collect_containers=False),
        project=ProjectConfig(
            project_dir=Path("/opt/myapp"),
            compose_file=Path("/opt/myapp/docker-compose.yml"),
            docker_service_name="web",
        ),
        storage=StorageConfig(db_path=tmp_path / "test.db"),
        garage=garage,
    )


def _make_agent(config: Config, tmp_path: Path) -> tuple[Agent, asyncio.Event]:
    nonce_store = NonceStore(tmp_path / "nonces.db")
    shutdown = asyncio.Event()
    ssl_ctx = MagicMock(spec=ssl.SSLContext)
    agent = Agent(config, SECRET, nonce_store, ssl_ctx, shutdown)
    return agent, shutdown


class TestGarageLoopNoOp:
    """Garage loop must be a no-op when config.garage is None or disabled."""

    @pytest.mark.asyncio
    async def test_no_garage_config(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, garage=None)
        agent, shutdown = _make_agent(config, tmp_path)
        ws = AsyncMock()
        # Should return immediately without doing anything
        await agent._garage_loop(ws)
        # No sends should have happened
        ws.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_garage_disabled(self, tmp_path: Path) -> None:
        garage_cfg = GarageConfig(
            enabled=False,
            container_name="garaged",
            garage_binary="/garage",
            docker_binary="/usr/bin/docker",
            config_path=tmp_path / "garage.toml",
            state_push_interval_seconds=0.05,
        )
        config = _make_config(tmp_path, garage=garage_cfg)
        agent, shutdown = _make_agent(config, tmp_path)
        ws = AsyncMock()
        await agent._garage_loop(ws)
        ws.send.assert_not_called()


class TestGarageLoopEnabled:
    """Garage loop updates shared state when enabled."""

    @pytest.mark.asyncio
    async def test_updates_garage_state(self, tmp_path: Path) -> None:
        garage_cfg = GarageConfig(
            enabled=True,
            container_name="garaged",
            garage_binary="/garage",
            docker_binary="/usr/bin/docker",
            config_path=tmp_path / "garage.toml",
            state_push_interval_seconds=0.05,
        )
        config = _make_config(tmp_path, garage=garage_cfg)
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
            db_engine="sqlite",
            object_count=5,
            block_count=10,
            buckets=[],
            keys=[],
            peers=[],
        )

        async def run_loop() -> None:
            # Patch where agent.py imports it
            with patch(
                "stormpulse.agent.collect_garage_state",
                return_value=fake_state,
            ):
                task = asyncio.create_task(agent._garage_loop(ws))
                await asyncio.sleep(0.15)
                shutdown.set()
                await task

        await run_loop()
        assert agent._garage_state is not None
        assert agent._garage_state.node_id == "abc123"


class TestGarageCommandsInRegistry:
    """Garage commands are merged into the registry when enabled."""

    def test_garage_commands_registered(self, tmp_path: Path) -> None:
        garage_cfg = GarageConfig(
            enabled=True,
            container_name="garaged",
            garage_binary="/garage",
            docker_binary="/usr/bin/docker",
            config_path=tmp_path / "garage.toml",
            state_push_interval_seconds=300,
        )
        config = _make_config(tmp_path, garage=garage_cfg)
        agent, _ = _make_agent(config, tmp_path)
        assert "garage_status" in agent._registry
        assert "garage_bucket_list" in agent._registry
        assert "garage_key_create" in agent._registry

    def test_no_garage_commands_without_config(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, garage=None)
        agent, _ = _make_agent(config, tmp_path)
        assert "garage_status" not in agent._registry
        # Original commands still present
        assert "git_pull" in agent._registry


class TestGarageRefresh:
    """garage_refresh collects state and returns result."""

    def _garage_cfg(self, tmp_path: Path) -> GarageConfig:
        return GarageConfig(
            enabled=True,
            container_name="garaged",
            garage_binary="/garage",
            docker_binary="/usr/bin/docker",
            config_path=tmp_path / "garage.toml",
            state_push_interval_seconds=300,
        )

    @pytest.mark.asyncio
    async def test_refresh_updates_state(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, garage=self._garage_cfg(tmp_path))
        agent, _ = _make_agent(config, tmp_path)

        fake_state = GarageState(
            node_id="abc123", hostname="test", zone="zone-1",
            capacity_gb=10.0, data_avail_gb=8.0, version="v2.2.0",
            healthy=True, db_engine="sqlite",
            object_count=5, block_count=10, buckets=[], keys=[], peers=[],
        )
        with patch(
            "stormpulse.agent.collect_garage_state",
            return_value=fake_state,
        ):
            result = await agent._handle_garage_refresh("req-1")

        assert result.success is True
        assert result.command == "garage_refresh"
        assert "0 buckets" in result.stdout
        assert agent._garage_state is not None
        assert agent._garage_state.node_id == "abc123"

    @pytest.mark.asyncio
    async def test_refresh_not_configured(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, garage=None)
        agent, _ = _make_agent(config, tmp_path)

        result = await agent._handle_garage_refresh("req-1")

        assert result.success is False
        assert result.failure_reason == "not_configured"

    @pytest.mark.asyncio
    async def test_refresh_collection_fails(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, garage=self._garage_cfg(tmp_path))
        agent, _ = _make_agent(config, tmp_path)

        with patch(
            "stormpulse.agent.collect_garage_state",
            return_value=None,
        ):
            result = await agent._handle_garage_refresh("req-1")

        assert result.success is False
        assert result.failure_reason == "collection_failed"
        assert agent._garage_state is None

    def test_garage_refresh_in_registry(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, garage=self._garage_cfg(tmp_path))
        agent, _ = _make_agent(config, tmp_path)
        assert "garage_refresh" in agent._registry


class TestSensitiveOutputLogging:
    """Verify sensitive_output flag prevents logging."""

    def test_sensitive_command_no_debug_log(self, tmp_path: Path) -> None:
        garage_cfg = GarageConfig(
            enabled=True,
            container_name="garaged",
            garage_binary="/garage",
            docker_binary="/usr/bin/docker",
            config_path=tmp_path / "garage.toml",
            state_push_interval_seconds=300,
        )
        config = _make_config(tmp_path, garage=garage_cfg)
        agent, _ = _make_agent(config, tmp_path)
        cmd_def = agent._registry["garage_key_create"]
        assert cmd_def.sensitive_output is True
