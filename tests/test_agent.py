"""Tests for stormpulse.agent."""

from __future__ import annotations

import asyncio
import json
import ssl
import time
import uuid
from collections.abc import Generator
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from websockets.exceptions import ConnectionClosed

from stormpulse.agent import Agent, _build_commands_metadata, _strip_binary_path, create_ssl_context
from stormpulse.auth import NonceStore, generate_nonce, sign
from stormpulse.config import (
    AgentConfig,
    AuthConfig,
    CommandDef,
    Config,
    DashboardConfig,
    GarageConfig,
    MetricsConfig,
    ParamDef,
    ProjectConfig,
    StorageConfig,
    TlsConfig,
)
from stormpulse.protocol import (
    CommandResultPayload,
    Envelope,
    MessageType,
    MetricsPayload,
    format_timestamp,
    make_heartbeat,
)
from stormpulse.auth import canonical_command_request, canonical_command_sequence


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SECRET = b"test-secret-key-256-bits-long!!!"

_DUMMY_PROJECT = ProjectConfig(
    project_dir=Path("/opt/myapp"),
    compose_file=Path("/opt/myapp/docker-compose.yml"),
    docker_service_name="web",
)


@pytest.fixture
def config(tmp_path: Path) -> Config:
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
    )


@pytest.fixture
def nonce_store(tmp_path: Path) -> Generator[NonceStore, None, None]:
    store = NonceStore(tmp_path / "nonces.db")
    yield store
    store.close()


@pytest.fixture
def shutdown() -> asyncio.Event:
    return asyncio.Event()


@pytest.fixture
def agent(config: Config, nonce_store: NonceStore, shutdown: asyncio.Event) -> Agent:
    ssl_ctx = MagicMock(spec=ssl.SSLContext)
    return Agent(config, SECRET, nonce_store, ssl_ctx, shutdown)


def _make_signed_request_json(
    command: str = "git_pull",
    agent_id: str = "test-01",
    params: dict[str, str] | None = None,
) -> str:
    """Build a signed command.request envelope as JSON string."""
    ts = datetime.now(timezone.utc)
    nonce = generate_nonce()
    ts_str = format_timestamp(ts)
    canonical = canonical_command_request(command, nonce, ts_str, params)
    sig = sign(canonical, SECRET)
    envelope = Envelope(
        v=1,
        type=MessageType.COMMAND_REQUEST,
        id=str(uuid.uuid4()),
        ts=ts,
        agent_id=agent_id,
        payload={"command": command, "params": params or {}, "hmac": sig, "nonce": nonce},
    )
    return envelope.to_json()


def _make_signed_sequence_json(
    commands: list[str] | None = None,
    agent_id: str = "test-01",
    stop_on_failure: bool = True,
) -> str:
    """Build a signed command.sequence envelope as JSON string."""
    if commands is None:
        commands = ["git_pull", "docker_logs"]
    ts = datetime.now(timezone.utc)
    nonce = generate_nonce()
    ts_str = format_timestamp(ts)
    sequence_id = str(uuid.uuid4())
    canonical = canonical_command_sequence(
        sequence_id, commands, stop_on_failure, nonce, ts_str,
    )
    sig = sign(canonical, SECRET)
    envelope = Envelope(
        v=1,
        type=MessageType.COMMAND_SEQUENCE,
        id=str(uuid.uuid4()),
        ts=ts,
        agent_id=agent_id,
        payload={
            "sequence_id": sequence_id,
            "commands": commands,
            "stop_on_failure": stop_on_failure,
            "hmac": sig,
            "nonce": nonce,
        },
    )
    return envelope.to_json()


def _make_result(
    command: str = "git_pull",
    success: bool = True,
    request_id: str = "r1",
    sequence_id: str | None = None,
) -> CommandResultPayload:
    return CommandResultPayload(
        request_id=request_id,
        command=command,
        group="deploy",
        success=success,
        exit_code=0 if success else 1,
        stdout="ok\n" if success else "",
        stderr="" if success else "fail\n",
        duration_ms=100,
        sequence_id=sequence_id,
        failure_reason=None if success else "exit_code",
    )


# ---------------------------------------------------------------------------
# SSL context
# ---------------------------------------------------------------------------


@patch("stormpulse.agent.ssl.create_default_context")
def test_create_ssl_context(mock_ctx_factory: MagicMock) -> None:
    mock_ctx = MagicMock(spec=ssl.SSLContext)
    mock_ctx_factory.return_value = mock_ctx
    tls = TlsConfig(
        ca_cert=Path("/ca.pem"),
        client_cert=Path("/agent.pem"),
        client_key=Path("/key.pem"),
    )
    result = create_ssl_context(tls)
    mock_ctx_factory.assert_called_once_with()
    mock_ctx.load_verify_locations.assert_called_once_with(cafile="/ca.pem")
    mock_ctx.load_cert_chain.assert_called_once_with(certfile="/agent.pem", keyfile="/key.pem")
    assert result is mock_ctx


# ---------------------------------------------------------------------------
# _strip_binary_path
# ---------------------------------------------------------------------------


def test_strip_binary_path_absolute() -> None:
    assert _strip_binary_path("/usr/bin/docker") == "docker"


def test_strip_binary_path_deep() -> None:
    assert _strip_binary_path("/usr/local/bin/git") == "git"


def test_strip_binary_path_relative_unchanged() -> None:
    assert _strip_binary_path("python") == "python"


def test_strip_binary_path_single_slash_unchanged() -> None:
    assert _strip_binary_path("/single") == "/single"


def test_strip_binary_path_placeholder_unchanged() -> None:
    assert _strip_binary_path("{project_dir}") == "{project_dir}"


def test_strip_binary_path_flag_unchanged() -> None:
    assert _strip_binary_path("--tail") == "--tail"


# ---------------------------------------------------------------------------
# _build_commands_metadata
# ---------------------------------------------------------------------------


def test_build_commands_metadata_basic() -> None:
    registry = {
        "git_pull": CommandDef(
            group="deploy",
            command=["/usr/bin/git", "-C", "{project_dir}", "pull"],
            timeout=60,
            description="Pull latest changes from remote",
        ),
    }
    result = _build_commands_metadata(registry, _DUMMY_PROJECT)
    assert "git_pull" in result
    entry = result["git_pull"]
    assert entry["group"] == "deploy"
    assert entry["description"] == "Pull latest changes from remote"
    assert entry["template"] == ["git", "-C", "{project_dir}", "pull"]
    assert entry["timeout"] == 60
    assert entry["requires_confirmation"] is False
    assert entry["long_running"] is False
    assert entry["params"] == {}


def test_build_commands_metadata_strips_paths() -> None:
    registry = {
        "docker_up": CommandDef(
            group="deploy",
            command=["/usr/bin/docker", "compose", "up", "-d"],
            timeout=120,
        ),
    }
    result = _build_commands_metadata(registry, _DUMMY_PROJECT)
    assert result["docker_up"]["template"][0] == "docker"
    assert result["docker_up"]["template"][1] == "compose"


def test_build_commands_metadata_sorted_keys() -> None:
    registry = {
        "z_cmd": CommandDef(group="z", command=["/bin/z"], timeout=10),
        "a_cmd": CommandDef(group="a", command=["/bin/a"], timeout=10),
    }
    result = _build_commands_metadata(registry, _DUMMY_PROJECT)
    assert list(result.keys()) == ["a_cmd", "z_cmd"]


def test_build_commands_metadata_with_params() -> None:
    registry = {
        "docker_logs": CommandDef(
            group="diagnostics",
            command=["/usr/bin/docker", "logs", "{service}"],
            timeout=30,
            description="Show logs",
            params={
                "service": ParamDef(
                    placeholder="service",
                    default="web",
                    pattern="[a-zA-Z0-9_-]+",
                    description="Docker Compose service name",
                ),
            },
        ),
    }
    result = _build_commands_metadata(registry, _DUMMY_PROJECT)
    params = result["docker_logs"]["params"]
    assert "service" in params
    assert params["service"] == {
        "default": "web",
        "pattern": "[a-zA-Z0-9_-]+",
        "description": "Docker Compose service name",
    }
    assert "placeholder" not in params["service"]


def test_build_commands_metadata_param_no_default() -> None:
    registry = {
        "logs": CommandDef(
            group="diagnostics",
            command=["/usr/bin/docker", "logs", "{service}"],
            timeout=30,
            params={
                "service": ParamDef(
                    placeholder="service",
                    default=None,
                    pattern="[a-z]+",
                ),
            },
        ),
    }
    result = _build_commands_metadata(registry, _DUMMY_PROJECT)
    assert result["logs"]["params"]["service"]["default"] is None


def test_build_commands_metadata_param_default_from_config() -> None:
    """Params with no static default get their default from project config."""
    registry = {
        "docker_logs": CommandDef(
            group="diagnostics",
            command=["/usr/bin/docker", "logs", "{docker_service_name}"],
            timeout=30,
            params={
                "docker_service_name": ParamDef(
                    placeholder="docker_service_name",
                    default=None,
                    pattern="[a-zA-Z0-9_-]+",
                    description="Docker Compose service name",
                ),
            },
        ),
    }
    result = _build_commands_metadata(registry, _DUMMY_PROJECT)
    # default should come from _DUMMY_PROJECT.docker_service_name ("web")
    assert result["docker_logs"]["params"]["docker_service_name"]["default"] == "web"


def test_build_commands_metadata_with_confirmation() -> None:
    registry = {
        "docker_down": CommandDef(
            group="deploy",
            command=["/usr/bin/docker", "compose", "down"],
            timeout=60,
            requires_confirmation=True,
            description="Stop containers",
        ),
    }
    result = _build_commands_metadata(registry, _DUMMY_PROJECT)
    assert result["docker_down"]["requires_confirmation"] is True


def test_build_commands_metadata_empty_registry() -> None:
    assert _build_commands_metadata({}, _DUMMY_PROJECT) == {}


# ---------------------------------------------------------------------------
# Heartbeat loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_loop_sends_messages(agent: Agent, shutdown: asyncio.Event) -> None:
    ws = AsyncMock()
    sent: list[str] = []
    ws.send = AsyncMock(side_effect=lambda msg: sent.append(msg))

    async def stop_after_delay() -> None:
        await asyncio.sleep(0.15)
        shutdown.set()

    await asyncio.gather(
        agent._heartbeat_loop(ws),
        stop_after_delay(),
    )
    assert len(sent) >= 2
    for msg in sent:
        data = json.loads(msg)
        assert data["type"] == "heartbeat"


@pytest.mark.asyncio
async def test_heartbeat_loop_stops_on_shutdown(agent: Agent, shutdown: asyncio.Event) -> None:
    ws = AsyncMock()
    shutdown.set()
    await agent._heartbeat_loop(ws)
    ws.send.assert_not_called()


# ---------------------------------------------------------------------------
# Metrics loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("stormpulse.agent.collect_metrics")
async def test_metrics_loop_sends_metrics(
    mock_collect: MagicMock, agent: Agent, shutdown: asyncio.Event,
) -> None:
    mock_collect.return_value = MetricsPayload(
        cpu_percent=10.0, memory_percent=50.0, memory_used_mb=512.0,
        memory_total_mb=1024.0, disk_percent=30.0, disk_used_gb=8.0,
        disk_total_gb=40.0, load_avg_1m=0.5, load_avg_5m=0.3,
        uptime_seconds=3600.0, containers=[],
    )
    ws = AsyncMock()
    sent: list[str] = []
    ws.send = AsyncMock(side_effect=lambda msg: sent.append(msg))

    async def stop_after_delay() -> None:
        await asyncio.sleep(0.15)
        shutdown.set()

    await asyncio.gather(
        agent._metrics_loop(ws),
        stop_after_delay(),
    )
    assert len(sent) >= 2
    for msg in sent:
        data = json.loads(msg)
        assert data["type"] == "metrics.push"
    assert mock_collect.call_count >= 2


@pytest.mark.asyncio
@patch("stormpulse.agent.collect_metrics")
async def test_metrics_loop_survives_collection_error(
    mock_collect: MagicMock, agent: Agent, shutdown: asyncio.Event,
) -> None:
    call_count = 0

    def side_effect(*args: object) -> MetricsPayload:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("psutil broke")
        return MetricsPayload(
            cpu_percent=1.0, memory_percent=2.0, memory_used_mb=3.0,
            memory_total_mb=4.0, disk_percent=5.0, disk_used_gb=6.0,
            disk_total_gb=7.0, load_avg_1m=0.0, load_avg_5m=0.0,
            uptime_seconds=1.0, containers=[],
        )

    mock_collect.side_effect = side_effect
    ws = AsyncMock()

    async def stop_after_delay() -> None:
        await asyncio.sleep(0.15)
        shutdown.set()

    await asyncio.gather(
        agent._metrics_loop(ws),
        stop_after_delay(),
    )
    # Should have continued past the error and sent at least one metrics push
    assert ws.send.call_count >= 1


# ---------------------------------------------------------------------------
# Dispatch — command request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("stormpulse.agent.execute_command")
async def test_dispatch_command_request(
    mock_exec: MagicMock, agent: Agent,
) -> None:
    mock_exec.return_value = _make_result()
    ws = AsyncMock()
    raw = _make_signed_request_json()

    await agent._dispatch(ws, raw)

    mock_exec.assert_called_once()
    ws.send.assert_called_once()
    sent_data = json.loads(ws.send.call_args[0][0])
    assert sent_data["type"] == "command.result"
    assert sent_data["payload"]["success"] is True


@pytest.mark.asyncio
async def test_dispatch_bad_json(agent: Agent) -> None:
    ws = AsyncMock()
    await agent._dispatch(ws, "not json{{{")
    ws.send.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_bad_hmac(agent: Agent) -> None:
    ws = AsyncMock()
    ts = datetime.now(timezone.utc)
    envelope = Envelope(
        v=1,
        type=MessageType.COMMAND_REQUEST,
        id=str(uuid.uuid4()),
        ts=ts,
        agent_id="test-01",
        payload={"command": "git_pull", "params": {}, "hmac": "bad", "nonce": "n"},
    )
    await agent._dispatch(ws, envelope.to_json())
    ws.send.assert_not_called()


# ---------------------------------------------------------------------------
# Dispatch — command sequence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("stormpulse.agent.execute_command")
async def test_dispatch_command_sequence(
    mock_exec: MagicMock, agent: Agent,
) -> None:
    mock_exec.return_value = _make_result()
    ws = AsyncMock()
    raw = _make_signed_sequence_json(commands=["git_pull", "docker_logs"])

    await agent._dispatch(ws, raw)

    assert mock_exec.call_count == 2
    assert ws.send.call_count == 2


@pytest.mark.asyncio
@patch("stormpulse.agent.execute_command")
async def test_dispatch_sequence_stop_on_failure(
    mock_exec: MagicMock, agent: Agent,
) -> None:
    mock_exec.return_value = _make_result(success=False)
    ws = AsyncMock()
    raw = _make_signed_sequence_json(
        commands=["git_pull", "docker_logs"],
        stop_on_failure=True,
    )

    await agent._dispatch(ws, raw)

    # Should stop after first failure
    assert mock_exec.call_count == 1
    assert ws.send.call_count == 1


# ---------------------------------------------------------------------------
# Reconnection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_reconnects_on_connection_error(
    config: Config, nonce_store: NonceStore,
) -> None:
    shutdown = asyncio.Event()
    ssl_ctx = MagicMock(spec=ssl.SSLContext)
    agent = Agent(config, SECRET, nonce_store, ssl_ctx, shutdown)
    attempt_count = 0

    def mock_connect_factory(*args: object, **kwargs: object) -> MagicMock:
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count >= 3:
            shutdown.set()
        raise OSError("Connection refused")

    with patch("stormpulse.agent.connect", side_effect=mock_connect_factory):
        await agent.run()

    assert attempt_count >= 2


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_exits_on_shutdown(
    config: Config, nonce_store: NonceStore,
) -> None:
    shutdown = asyncio.Event()
    ssl_ctx = MagicMock(spec=ssl.SSLContext)
    agent = Agent(config, SECRET, nonce_store, ssl_ctx, shutdown)

    shutdown.set()
    await agent.run()
    # Should exit immediately without attempting to connect


# ---------------------------------------------------------------------------
# Unexpected message types
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_unexpected_type(agent: Agent) -> None:
    ws = AsyncMock()
    heartbeat = make_heartbeat("test-01")
    await agent._dispatch(ws, heartbeat.to_json())
    ws.send.assert_not_called()


# ---------------------------------------------------------------------------
# Dashboard acknowledgements (silently ignored)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# garage_refresh command
# ---------------------------------------------------------------------------


def _garage_cfg(tmp_path: Path, enabled: bool = True) -> GarageConfig:
    return GarageConfig(
        enabled=enabled,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=tmp_path / "garage.toml",
        state_push_interval_seconds=0.05,
    )


def _config_with_garage(base: Config, garage: GarageConfig) -> Config:
    # Config is frozen; rebuild with garage set.
    from dataclasses import replace
    return replace(base, garage=garage)


@pytest.mark.asyncio
@patch("stormpulse.agent.collect_metrics")
@patch("stormpulse.agent.collect_garage_state")
async def test_garage_refresh_command_success(
    mock_collect: MagicMock, mock_metrics: MagicMock,
    config: Config, nonce_store: NonceStore, shutdown: asyncio.Event, tmp_path: Path,
) -> None:
    from stormpulse.garage.state import GarageState
    fake_state = GarageState(
        node_id="n1", hostname="h", zone="z", capacity_gb=1.0, data_avail_gb=1.0,
        version="v", healthy=True, db_engine="x", object_count=0, block_count=0,
        buckets=[], keys=[], peers=[],
    )
    mock_collect.return_value = fake_state
    mock_metrics.return_value = MetricsPayload(
        cpu_percent=0, memory_percent=0, memory_used_mb=0, memory_total_mb=0,
        disk_percent=0, disk_used_gb=0, disk_total_gb=0, load_avg_1m=0,
        load_avg_5m=0, uptime_seconds=0, containers=[],
    )
    cfg = _config_with_garage(config, _garage_cfg(tmp_path))
    ssl_ctx = MagicMock(spec=ssl.SSLContext)
    ag = Agent(cfg, SECRET, nonce_store, ssl_ctx, shutdown)
    ws = AsyncMock()
    raw = _make_signed_request_json(command="garage_refresh")

    await ag._dispatch(ws, raw)

    # First send = command.result, second send = immediate metrics push
    assert ws.send.call_count == 2
    result_env = json.loads(ws.send.call_args_list[0][0][0])
    metrics_env = json.loads(ws.send.call_args_list[1][0][0])
    assert result_env["type"] == "command.result"
    assert result_env["payload"]["success"] is True
    assert metrics_env["type"] == "metrics.push"
    assert ag._garage_state is fake_state


@pytest.mark.asyncio
async def test_garage_refresh_when_disabled_returns_failure(
    config: Config, nonce_store: NonceStore, shutdown: asyncio.Event, tmp_path: Path,
) -> None:
    cfg = _config_with_garage(config, _garage_cfg(tmp_path, enabled=False))
    ssl_ctx = MagicMock(spec=ssl.SSLContext)
    ag = Agent(cfg, SECRET, nonce_store, ssl_ctx, shutdown)

    result = await ag._handle_garage_refresh("req-1")
    assert result.success is False
    assert result.failure_reason == "not_configured"


@pytest.mark.asyncio
@patch("stormpulse.agent.collect_garage_state", return_value=None)
async def test_garage_refresh_collection_failure(
    _mock: MagicMock,
    config: Config, nonce_store: NonceStore, shutdown: asyncio.Event, tmp_path: Path,
) -> None:
    cfg = _config_with_garage(config, _garage_cfg(tmp_path))
    ag = Agent(cfg, SECRET, nonce_store, MagicMock(spec=ssl.SSLContext), shutdown)
    result = await ag._handle_garage_refresh("req-1")
    assert result.success is False
    assert result.failure_reason == "collection_failed"


# ---------------------------------------------------------------------------
# _garage_loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_garage_loop_noop_when_disabled(
    config: Config, nonce_store: NonceStore, shutdown: asyncio.Event, tmp_path: Path,
) -> None:
    cfg = _config_with_garage(config, _garage_cfg(tmp_path, enabled=False))
    ag = Agent(cfg, SECRET, nonce_store, MagicMock(spec=ssl.SSLContext), shutdown)
    ws = AsyncMock()
    # Should return immediately without touching collect_garage_state
    with patch("stormpulse.agent.collect_garage_state") as mock_collect:
        await ag._garage_loop(ws)
        mock_collect.assert_not_called()


@pytest.mark.asyncio
@patch("stormpulse.agent.collect_garage_state")
async def test_garage_loop_updates_state(
    mock_collect: MagicMock,
    config: Config, nonce_store: NonceStore, shutdown: asyncio.Event, tmp_path: Path,
) -> None:
    from stormpulse.garage.state import GarageState
    fake = GarageState(
        node_id="n1", hostname="h", zone="z", capacity_gb=1.0, data_avail_gb=1.0,
        version="v", healthy=True, db_engine="x", object_count=0, block_count=0,
        buckets=[], keys=[], peers=[],
    )
    mock_collect.return_value = fake
    cfg = _config_with_garage(config, _garage_cfg(tmp_path))
    ag = Agent(cfg, SECRET, nonce_store, MagicMock(spec=ssl.SSLContext), shutdown)
    ws = AsyncMock()

    async def stop_after_delay() -> None:
        await asyncio.sleep(0.12)
        shutdown.set()

    await asyncio.gather(ag._garage_loop(ws), stop_after_delay())
    assert ag._garage_state is fake
    assert mock_collect.call_count >= 1


# ---------------------------------------------------------------------------
# log.batch.ack handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_batch_ack_missing_batch_id(agent: Agent) -> None:
    envelope = Envelope(
        v=1, type=MessageType.LOG_BATCH_ACK, id="x",
        ts=datetime.now(timezone.utc), agent_id="test-01",
        payload={},  # no batch_id
    )
    # Should not raise
    await agent._handle_log_batch_ack(envelope)


@pytest.mark.asyncio
async def test_log_batch_ack_unknown_batch_is_noop(agent: Agent) -> None:
    envelope = Envelope(
        v=1, type=MessageType.LOG_BATCH_ACK, id="x",
        ts=datetime.now(timezone.utc), agent_id="test-01",
        payload={"batch_id": "never-sent"},
    )
    await agent._handle_log_batch_ack(envelope)  # should silently ignore


@pytest.mark.asyncio
async def test_log_batch_ack_advances_position(agent: Agent) -> None:
    # Inject a fake pending batch + shipper
    fake_shipper = MagicMock()
    fake_shipper.tailer.confirm_shipped = MagicMock()
    agent._shippers["grp"] = fake_shipper
    agent._pending_batches["bid-1"] = ("grp", 4242, time.monotonic())

    envelope = Envelope(
        v=1, type=MessageType.LOG_BATCH_ACK, id="x",
        ts=datetime.now(timezone.utc), agent_id="test-01",
        payload={"batch_id": "bid-1"},
    )
    await agent._handle_log_batch_ack(envelope)
    fake_shipper.tailer.confirm_shipped.assert_called_once_with(4242)
    assert "bid-1" not in agent._pending_batches


# ---------------------------------------------------------------------------
# Sequence — continue-through-failure + invalid command
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("stormpulse.agent.execute_command")
async def test_dispatch_sequence_continues_past_failure(
    mock_exec: MagicMock, agent: Agent,
) -> None:
    mock_exec.return_value = _make_result(success=False)
    ws = AsyncMock()
    raw = _make_signed_sequence_json(
        commands=["git_pull", "docker_logs"],
        stop_on_failure=False,
    )
    await agent._dispatch(ws, raw)
    # Both commands executed despite first failing
    assert mock_exec.call_count == 2
    assert ws.send.call_count == 2


@pytest.mark.asyncio
@patch("stormpulse.agent.execute_command")
async def test_dispatch_sequence_invalid_command_aborts(
    mock_exec: MagicMock, agent: Agent,
) -> None:
    ws = AsyncMock()
    raw = _make_signed_sequence_json(commands=["this_command_does_not_exist"])
    await agent._dispatch(ws, raw)
    mock_exec.assert_not_called()
    ws.send.assert_not_called()


# ---------------------------------------------------------------------------
# PulseLogger integration on command result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("stormpulse.agent.execute_command")
async def test_command_result_logged_to_pulse_logger(
    mock_exec: MagicMock,
    config: Config, nonce_store: NonceStore, shutdown: asyncio.Event,
) -> None:
    mock_exec.return_value = _make_result()
    pulse_logger = MagicMock()
    ag = Agent(
        config, SECRET, nonce_store, MagicMock(spec=ssl.SSLContext), shutdown,
        pulse_logger=pulse_logger,
    )
    ws = AsyncMock()
    raw = _make_signed_request_json()
    await ag._dispatch(ws, raw)
    pulse_logger.log_command_result.assert_called_once()
    kwargs = pulse_logger.log_command_result.call_args.kwargs
    assert kwargs["command"] == "git_pull"
    assert kwargs["success"] is True


# ---------------------------------------------------------------------------
# Reconnect loop — ConnectionClosed and generic Exception branches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_reconnects_on_connection_closed(
    config: Config, nonce_store: NonceStore,
) -> None:
    shutdown = asyncio.Event()
    ag = Agent(config, SECRET, nonce_store, MagicMock(spec=ssl.SSLContext), shutdown)
    attempts = 0

    def mock_connect(*a: object, **kw: object) -> MagicMock:
        nonlocal attempts
        attempts += 1
        if attempts >= 2:
            shutdown.set()
        raise ConnectionClosed(None, None)

    with patch("stormpulse.agent.connect", side_effect=mock_connect):
        await ag.run()
    assert attempts >= 2


@pytest.mark.asyncio
async def test_run_handles_unexpected_exception(
    config: Config, nonce_store: NonceStore,
) -> None:
    shutdown = asyncio.Event()
    ag = Agent(config, SECRET, nonce_store, MagicMock(spec=ssl.SSLContext), shutdown)
    attempts = 0

    def mock_connect(*a: object, **kw: object) -> MagicMock:
        nonlocal attempts
        attempts += 1
        if attempts >= 1:
            shutdown.set()
        raise RuntimeError("totally unexpected")

    with patch("stormpulse.agent.connect", side_effect=mock_connect):
        await ag.run()
    assert attempts >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("ack_type", [
    MessageType.REGISTER_OK,
    MessageType.HEARTBEAT_ACK,
    MessageType.METRICS_ACK,
    MessageType.COMMAND_RESULT_ACK,
    MessageType.ERROR,
])
async def test_dispatch_ack_types_ignored(agent: Agent, ack_type: MessageType) -> None:
    ws = AsyncMock()
    envelope = Envelope(
        v=1,
        type=ack_type,
        id=str(uuid.uuid4()),
        ts=datetime.now(timezone.utc),
        agent_id="test-01",
        payload={},
    )
    await agent._dispatch(ws, envelope.to_json())
    ws.send.assert_not_called()


# ---------------------------------------------------------------------------
# Long-running dispatch param validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_long_running_dispatch_rejects_params_failing_regex(agent: Agent) -> None:
    """Long-running path must enforce CommandDef param patterns the same way
    the subprocess path does. A bucket_name that doesn't match the registry
    regex should produce a structured failure result without ever invoking
    the handler factory.
    """
    from stormpulse.commands.jobs import JobManager
    from stormpulse.protocol import CommandRequestPayload

    test_cmd = CommandDef(
        group="test",
        command=["test_long_running"],
        timeout=60,
        long_running=True,
        params={
            "bucket_name": ParamDef(
                placeholder="bucket_name",
                default=None,
                pattern=r"[a-z0-9_-]+",  # lowercase, digits, underscore, hyphen
                description="bucket",
            ),
        },
    )
    agent._registry["test_long_running"] = test_cmd

    sent: list[Envelope] = []

    async def fake_send(env: Envelope) -> None:
        sent.append(env)

    agent._job_manager = JobManager(agent._config.agent.id, fake_send)

    # Track whether the handler factory was reached
    factory_invocations: list[str] = []

    def fake_factory(command: str, params: dict[str, str]) -> None:
        factory_invocations.append(command)
        return None  # never reached if validation does its job

    agent._make_long_running_handler = fake_factory  # type: ignore[method-assign]

    # Path-traversal-shaped value violates the lowercase-only pattern
    payload = CommandRequestPayload(
        command="test_long_running",
        params={"bucket_name": "../etc/passwd"},
        hmac="x",
        nonce="x",
    )
    await agent._dispatch_long_running("req-bad", payload, test_cmd)

    # Failure result emitted; handler factory never invoked
    assert factory_invocations == []
    assert len(sent) == 1
    failure = sent[0]
    assert failure.type == MessageType.COMMAND_RESULT
    assert failure.payload["request_id"] == "req-bad"
    assert failure.payload["success"] is False
    assert failure.payload["failure_reason"] == "os_error"
    assert "does not match pattern" in failure.payload["stderr"]

    await agent._job_manager.shutdown_all()


@pytest.mark.asyncio
async def test_long_running_dispatch_passes_validated_params_to_factory(agent: Agent) -> None:
    """When params validate, they reach the handler factory (with defaults
    merged in by validate_params)."""
    from stormpulse.commands.jobs import JobManager
    from stormpulse.protocol import CommandRequestPayload

    test_cmd = CommandDef(
        group="test",
        command=["test_long_running"],
        timeout=60,
        long_running=True,
        params={
            "bucket_name": ParamDef(
                placeholder="bucket_name",
                default=None,
                pattern=r"[a-z0-9_-]+",
                description="bucket",
            ),
            "mode": ParamDef(
                placeholder="mode",
                default="fast",
                pattern=r"fast|slow",
                description="mode",
            ),
        },
    )
    agent._registry["test_long_running"] = test_cmd

    async def fake_send(env: Envelope) -> None:
        pass

    agent._job_manager = JobManager(agent._config.agent.id, fake_send)

    captured_params: dict[str, dict[str, str]] = {}

    def fake_factory(command: str, params: dict[str, str]) -> None:
        captured_params[command] = dict(params)
        return None

    agent._make_long_running_handler = fake_factory  # type: ignore[method-assign]

    payload = CommandRequestPayload(
        command="test_long_running",
        params={"bucket_name": "valid-bucket"},  # mode omitted -> default
        hmac="x",
        nonce="x",
    )
    await agent._dispatch_long_running("req-ok", payload, test_cmd)

    assert "test_long_running" in captured_params
    # Default merged in
    assert captured_params["test_long_running"] == {
        "bucket_name": "valid-bucket",
        "mode": "fast",
    }

    await agent._job_manager.shutdown_all()


# ---------------------------------------------------------------------------
# garage_bucket_set_cors dispatch integration
#
# Bridges the unit tests on run_set_cors (in tests/garage/test_set_cors.py)
# and Garage-side smoke. These catch wired-up-wrong bugs in the dispatch
# path: the agent's registry must validate the origins regex before the
# handler factory runs, and validated params must reach the factory with
# the JSON-string origins intact (the factory decodes; the dispatcher does
# not).
# ---------------------------------------------------------------------------


_SET_CORS_VALID_PARAMS = {
    "bucket_name": "media",
    "s3_endpoint": "http://localhost:3900",
    "region": "garage",
    "access_key_id": "GK1",
    "secret_access_key": "secret",
    "origins": '["https://stormdevelopments.ca"]',
}


def _set_cors_cmd_def() -> CommandDef:
    """Pull the real garage_bucket_set_cors CommandDef from the garage builder.

    The agent fixture has no garage config so the registry doesn't include
    garage commands by default. Same shape as how the precedent dispatch
    tests inject a synthetic CommandDef — this just uses the production one.
    """
    from pathlib import Path
    from stormpulse.config import GarageConfig
    from stormpulse.garage.commands import build_garage_commands

    cfg = GarageConfig(
        enabled=True,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=Path("/opt/garage/garage.toml"),
        state_push_interval_seconds=300,
    )
    return build_garage_commands(cfg)["garage_bucket_set_cors"]


@pytest.mark.asyncio
async def test_garage_bucket_set_cors_dispatch_validates_origins_pattern(
    agent: Agent,
) -> None:
    """A non-bracketed origins value violates the registry pattern; the
    dispatcher must emit a structured failure before the factory runs."""
    from stormpulse.commands.jobs import JobManager
    from stormpulse.protocol import CommandRequestPayload

    cmd_def = _set_cors_cmd_def()
    agent._registry["garage_bucket_set_cors"] = cmd_def

    sent: list[Envelope] = []

    async def fake_send(env: Envelope) -> None:
        sent.append(env)

    agent._job_manager = JobManager(agent._config.agent.id, fake_send)

    factory_invocations: list[str] = []

    def fake_factory(command: str, params: dict[str, str]) -> None:
        factory_invocations.append(command)
        return None

    agent._make_long_running_handler = fake_factory  # type: ignore[method-assign]

    bad_params = dict(_SET_CORS_VALID_PARAMS)
    bad_params["origins"] = "not-bracketed"
    payload = CommandRequestPayload(
        command="garage_bucket_set_cors",
        params=bad_params,
        hmac="x",
        nonce="x",
    )
    await agent._dispatch_long_running("req-bad-origins", payload, cmd_def)

    assert factory_invocations == []
    assert len(sent) == 1
    failure = sent[0]
    assert failure.type == MessageType.COMMAND_RESULT
    assert failure.payload["request_id"] == "req-bad-origins"
    assert failure.payload["success"] is False
    assert failure.payload["failure_reason"] == "os_error"
    assert "does not match pattern" in failure.payload["stderr"]

    await agent._job_manager.shutdown_all()


@pytest.mark.asyncio
async def test_garage_bucket_set_cors_dispatch_passes_json_origins_to_factory(
    agent: Agent,
) -> None:
    """Valid params reach the factory with origins still as a JSON string —
    the factory decodes; the dispatcher just shuttles strings."""
    from stormpulse.commands.jobs import JobManager
    from stormpulse.protocol import CommandRequestPayload

    cmd_def = _set_cors_cmd_def()
    agent._registry["garage_bucket_set_cors"] = cmd_def

    async def fake_send(env: Envelope) -> None:
        pass

    agent._job_manager = JobManager(agent._config.agent.id, fake_send)

    captured_params: dict[str, dict[str, str]] = {}

    def fake_factory(command: str, params: dict[str, str]) -> None:
        captured_params[command] = dict(params)
        return None

    agent._make_long_running_handler = fake_factory  # type: ignore[method-assign]

    payload = CommandRequestPayload(
        command="garage_bucket_set_cors",
        params=dict(_SET_CORS_VALID_PARAMS),
        hmac="x",
        nonce="x",
    )
    await agent._dispatch_long_running("req-ok", payload, cmd_def)

    assert "garage_bucket_set_cors" in captured_params
    seen = captured_params["garage_bucket_set_cors"]
    # All six params reach the factory verbatim
    assert seen == _SET_CORS_VALID_PARAMS
    # origins is still a JSON string at the dispatcher boundary
    assert isinstance(seen["origins"], str)
    assert seen["origins"].startswith("[") and seen["origins"].endswith("]")

    await agent._job_manager.shutdown_all()
