"""Tests for garage-related agent behavior."""

from __future__ import annotations

import asyncio
import ssl
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from stormpulse.agent import Agent, garage_actions, loops
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
from stormpulse.signoff import SignoffState

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
    agent = Agent(
        config,
        SECRET,
        nonce_store,
        ssl_ctx,
        shutdown,
        signoff_state=SignoffState(config.storage.db_path.parent),
    )
    return agent, shutdown


class TestGarageLiveGate:
    """Bootstrap publishes ``garage_live`` so runtime gates have one bool to read."""

    def test_garage_live_false_without_config(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, garage=None)
        agent, _ = _make_agent(config, tmp_path)
        assert agent.garage_live is False

    def test_garage_live_false_when_disabled(self, tmp_path: Path) -> None:
        garage_cfg = GarageConfig(
            enabled=False,
            container_name="garaged",
            garage_binary="/garage",
            docker_binary="/usr/bin/docker",
            config_path=tmp_path / "garage.toml",
            state_push_interval_seconds=0.05,
        )
        config = _make_config(tmp_path, garage=garage_cfg)
        agent, _ = _make_agent(config, tmp_path)
        assert agent.garage_live is False

    def test_garage_live_true_when_enabled_and_preconditions_pass(
        self, tmp_path: Path
    ) -> None:
        # Preconditions are stubbed to pass by the autouse fixture in
        # tests/conftest.py.
        garage_cfg = GarageConfig(
            enabled=True,
            container_name="garaged",
            garage_binary="/garage",
            docker_binary="/usr/bin/docker",
            config_path=tmp_path / "garage.toml",
            state_push_interval_seconds=0.05,
        )
        config = _make_config(tmp_path, garage=garage_cfg)
        agent, _ = _make_agent(config, tmp_path)
        assert agent.garage_live is True


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
            with patch(
                "stormpulse.agent.loops.collect_garage_state",
                return_value=fake_state,
            ):
                task = asyncio.create_task(loops.garage_loop(agent, ws))
                await asyncio.sleep(0.15)
                shutdown.set()
                await task

        await run_loop()
        assert agent.garage_state is not None
        assert agent.garage_state.node_id == "abc123"

    @pytest.mark.asyncio
    async def test_collects_before_first_wait(self, tmp_path: Path) -> None:
        """First action in _garage_loop must be collect, not sleep."""
        garage_cfg = GarageConfig(
            enabled=True,
            container_name="garaged",
            garage_binary="/garage",
            docker_binary="/usr/bin/docker",
            config_path=tmp_path / "garage.toml",
            # Long interval - if loop waits first, we'd time out
            state_push_interval_seconds=600,
        )
        config = _make_config(tmp_path, garage=garage_cfg)
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
            db_engine="sqlite",
            object_count=5,
            block_count=10,
            buckets=[],
            keys=[],
            peers=[],
        )

        async def run_loop() -> None:
            with patch(
                "stormpulse.agent.loops.collect_garage_state",
                return_value=fake_state,
            ) as mock_collect:
                task = asyncio.create_task(loops.garage_loop(agent, ws))
                # Give just enough time for the collect to run, but nowhere
                # near 600s - proves collect happens before the wait
                await asyncio.sleep(0.1)
                assert mock_collect.called, (
                    "collect_garage_state was not called before wait"
                )
                shutdown.set()
                await task

        await run_loop()
        assert agent.garage_state is not None
        assert agent.garage_state.node_id == "immediate"


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
        with patch(
            "stormpulse.agent.garage_actions.collect_garage_state",
            return_value=fake_state,
        ):
            result = await garage_actions.collect_refresh_result(agent, "req-1")

        assert result.success is True
        assert result.command == "garage_refresh"
        assert "0 buckets" in result.stdout
        assert agent.garage_state is not None
        assert agent.garage_state.node_id == "abc123"

    @pytest.mark.asyncio
    async def test_refresh_not_configured(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, garage=None)
        agent, _ = _make_agent(config, tmp_path)

        result = await garage_actions.collect_refresh_result(agent, "req-1")

        assert result.success is False
        assert result.failure_reason == "not_configured"

    @pytest.mark.asyncio
    async def test_refresh_collection_fails(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, garage=self._garage_cfg(tmp_path))
        agent, _ = _make_agent(config, tmp_path)

        with patch(
            "stormpulse.agent.garage_actions.collect_garage_state",
            return_value=None,
        ):
            result = await garage_actions.collect_refresh_result(agent, "req-1")

        assert result.success is False
        assert result.failure_reason == "collection_failed"
        assert agent.garage_state is None

    def test_garage_refresh_in_registry(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, garage=self._garage_cfg(tmp_path))
        agent, _ = _make_agent(config, tmp_path)
        assert "garage_refresh" in agent.registry


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
        cmd_def = agent.registry["garage_key_create"]
        assert cmd_def.sensitive_output is True
