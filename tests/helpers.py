"""Shared test constants and helper functions."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from stormpulse.auth import (
    canonical_command_request,
    canonical_command_sequence,
    generate_nonce,
    sign,
)
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
from stormpulse.protocol import (
    CommandResultPayload,
    Envelope,
    MessageType,
    MetricsPayload,
    format_timestamp,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SECRET = b"test-secret-key-256-bits-long!!!"
AGENT_ID = "test-01"
PULSE_TOKEN = "tok-test-123"

FAKE_METRICS = MetricsPayload(
    cpu_percent=10.0,
    memory_percent=50.0,
    memory_used_mb=512.0,
    memory_total_mb=1024.0,
    disk_percent=30.0,
    disk_used_gb=8.0,
    disk_total_gb=40.0,
    load_avg_1m=0.5,
    load_avg_5m=0.3,
    uptime_seconds=3600.0,
    containers=[],
)

DUMMY_PROJECT = ProjectConfig(
    project_dir=Path("/opt/myapp"),
    compose_file=Path("/opt/myapp/docker-compose.yml"),
    docker_service_name="web",
)


def make_fake_garage_state() -> GarageState:
    """A minimal GarageState for tests that just need the shape."""
    return GarageState(
        node_id="n1",
        hostname="h",
        zone="z",
        capacity_gb=1.0,
        data_avail_gb=1.0,
        version="v",
        healthy=True,
        object_count=0,
        buckets=[],
        keys=[],
        peers=[],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_config(
    tmp_path: Path,
    port: int = 0,
    garage: GarageConfig | None = None,
) -> Config:
    """Build a Config pointing at ws://localhost:{port}/ws/ with fast intervals.

    ``port=0`` is fine for unit tests that don't actually connect — the URL
    is only consulted by the websocket client, which the tests mock.
    """
    return Config(
        agent=AgentConfig(id=AGENT_ID, pulse_token=PULSE_TOKEN),
        dashboard=DashboardConfig(
            url=f"ws://localhost:{port}/ws/",
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


def build_garage_config(tmp_path: Path) -> GarageConfig:
    """Build a valid GarageConfig with fake paths for testing."""
    config_path = tmp_path / "garage.toml"
    config_path.write_text("[s3_api]\napi_bind_addr = '[::]:3900'\n")
    return GarageConfig(
        enabled=True,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=config_path,
        state_push_interval_seconds=0.05,
    )


def sign_command_request(
    command: str = "git_pull",
    *,
    agent_id: str = AGENT_ID,
    nonce: str | None = None,
    ts: datetime | None = None,
    secret: bytes = SECRET,
    params: dict[str, str] | None = None,
) -> str:
    """Build a signed command.request envelope as a JSON string."""
    if ts is None:
        ts = datetime.now(UTC)
    if nonce is None:
        nonce = generate_nonce()
    ts_str = format_timestamp(ts)
    canonical = canonical_command_request(command, nonce, ts_str, params)
    sig = sign(canonical, secret)
    envelope = Envelope(
        v=1,
        type=MessageType.COMMAND_REQUEST,
        id=str(uuid.uuid4()),
        ts=ts,
        agent_id=agent_id,
        payload={
            "command": command,
            "params": params or {},
            "hmac": sig,
            "nonce": nonce,
        },
    )
    return envelope.to_json()


def sign_command_sequence(
    commands: list[str],
    *,
    stop_on_failure: bool = True,
    agent_id: str = AGENT_ID,
    nonce: str | None = None,
    ts: datetime | None = None,
    secret: bytes = SECRET,
    sequence_id: str | None = None,
) -> str:
    """Build a signed command.sequence envelope as a JSON string."""
    if ts is None:
        ts = datetime.now(UTC)
    if nonce is None:
        nonce = generate_nonce()
    if sequence_id is None:
        sequence_id = str(uuid.uuid4())
    ts_str = format_timestamp(ts)
    canonical = canonical_command_sequence(
        sequence_id,
        commands,
        stop_on_failure,
        nonce,
        ts_str,
    )
    sig = sign(canonical, secret)
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


def make_successful_result(
    command: str = "git_pull",
    request_id: str = "r1",
    sequence_id: str | None = None,
) -> CommandResultPayload:
    return CommandResultPayload(
        request_id=request_id,
        command=command,
        group="deploy",
        success=True,
        exit_code=0,
        stdout="ok\n",
        stderr="",
        duration_ms=50,
        sequence_id=sequence_id,
    )


def make_failed_result(
    command: str = "docker_logs",
    request_id: str = "r1",
    sequence_id: str | None = None,
) -> CommandResultPayload:
    return CommandResultPayload(
        request_id=request_id,
        command=command,
        group="deploy",
        success=False,
        exit_code=1,
        stdout="",
        stderr="error\n",
        duration_ms=50,
        sequence_id=sequence_id,
        failure_reason="exit_code",
    )
