"""Tests for stormpulse.agent."""

from __future__ import annotations

import asyncio
import json
import ssl
import uuid
from collections.abc import Generator
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from stormpulse.agent import Agent, _build_commands_metadata, _strip_binary_path, create_ssl_context
from stormpulse.auth import NonceStore, generate_nonce, sign
from stormpulse.config import (
    AgentConfig,
    AuthConfig,
    CommandDef,
    Config,
    DashboardConfig,
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
