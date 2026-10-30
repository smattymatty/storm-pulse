"""Shared test constants and helper functions."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from stormpulse.auth import generate_nonce, sign_envelope
from stormpulse.caddy.config import CaddyConfig
from stormpulse.config import (
    AgentConfig,
    AuthConfig,
    Config,
    DashboardConfig,
    MetricsConfig,
    ProjectConfig,
    StorageConfig,
    TlsConfig,
)
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.state import GarageBucket, GarageKeyRef, GarageState
from stormpulse.protocol import (
    SPEAKS,
    CommandResultPayload,
    Envelope,
    FailureReason,
    MessageType,
    MetricsPayload,
)
from stormpulse.signoff import SignoffState

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SECRET = b"test-secret-key-256-bits-long!!!"
AGENT_ID = "test-01"
PULSE_TOKEN = "tok-test-123"
# Digests with the real shape and no real meaning: fixtures only.
CONTRACT_DIGEST = "sha256:" + "0f" * 32
WIRE_DIGEST = "sha256:" + "1f" * 32


def unsealed_state(state_dir: Path) -> SignoffState:
    """An opened hatch, as `stormpulse signoff unseal` leaves it; fresh dirs are sealed."""
    state = SignoffState(state_dir)
    state.unseal()
    return state


def matched_test_session(agent: Any) -> Any:
    """Unit dispatch tests operate on a registered, compatible session."""
    from stormpulse.agent.session import SessionCompatibility

    agent.session_compatibility = SessionCompatibility(CONTRACT_DIGEST, CONTRACT_DIGEST)
    return agent


def register_fields(**overrides: Any) -> dict[str, Any]:
    """Every ``make_register`` keyword at its empty value: a quiet, sealed node.
    Override only what a test asserts on."""
    return {
        "contract_digest": CONTRACT_DIGEST,
        "wire_contract": WIRE_DIGEST,
        "commands": {},
        "integrations": {},
        "log_groups": [],
        "system_inventory": {},
        "signoff_sealed": True,
        "unsealed_since": None,
        "boot_id": "b0ot-1d",
        "agent_started_at": "2026-02-21T11:00:00Z",
        "components": [],
        "in_flight": [],
    } | overrides


def register_payload(**overrides: Any) -> dict[str, Any]:
    """A full ``register`` payload as the wire carries it; see ``register_fields``."""
    return {
        "version": "0.1.0",
        "pulse_token": PULSE_TOKEN,
        "speaks": list(SPEAKS),
        **register_fields(),
    } | overrides


class FakeWire:
    """Captures envelopes the manager would put on the wire.

    Tests assert against ``sent``. Set ``raise_on_send`` to simulate a
    closed connection.
    """

    def __init__(self) -> None:
        self.sent: list[Envelope] = []
        self.raise_on_send: Exception | None = None

    async def send(self, envelope: Envelope) -> None:
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.sent.append(envelope)


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


def make_garage_bucket(
    bucket_id: str,
    *,
    alias: str = "",
    size_bytes: int = 0,
    object_count: int = 0,
    keys: list[GarageKeyRef] | None = None,
) -> GarageBucket:
    """A GarageBucket with test-friendly defaults; override only what a test asserts on."""
    return GarageBucket(
        id=bucket_id,
        alias=alias,
        size_bytes=size_bytes,
        object_count=object_count,
        keys=keys or [],
        website_access=False,
        website_index_document="index.html",
        website_error_document=None,
        quota_max_size_bytes=None,
        quota_max_objects=None,
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


def set_garage_state(agent: object, state: object) -> None:
    """Set the live garage Integration runtime's state (CORE-005 generic runtime)."""
    agent.integrations["garage"].state = state  # type: ignore[attr-defined]


def get_garage_state(agent: object) -> object:
    """Return the garage Integration runtime's state, or None if garage is absent."""
    rt = agent.integrations.get("garage")  # type: ignore[attr-defined]
    return rt.state if rt is not None else None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def garage_raw_table(garage: GarageConfig) -> dict[str, object]:
    """Serialize a GarageConfig back into the raw [garage] table bootstrap parses.

    CORE-005: Config holds raw integration tables keyed by id, and each
    Integration parses its own. Tests still build a typed GarageConfig for
    ergonomics; this round-trips it into the table the bootstrap loop expects.
    """
    table: dict[str, object] = {
        "enabled": garage.enabled,
        "container_name": garage.container_name,
        "garage_binary": garage.garage_binary,
        "docker_binary": garage.docker_binary,
        "config_path": str(garage.config_path),
    }
    if garage.admin_url:
        table["admin_url"] = garage.admin_url
    if garage.admin_token:
        table["admin_token"] = garage.admin_token
    return table


def caddy_raw_table(caddy: CaddyConfig) -> dict[str, object]:
    """Serialize a CaddyConfig back into the raw [caddy] table bootstrap parses."""
    return {
        "enabled": caddy.enabled,
        "admin_url": caddy.admin_url,
        "main_caddyfile": str(caddy.main_caddyfile),
        "drop_in_path": str(caddy.drop_in_path),
    }


def build_config(
    tmp_path: Path,
    port: int = 0,
    garage: GarageConfig | None = None,
    integrations: dict[str, dict[str, object]] | None = None,
    metrics_push_interval: float = 0.05,
) -> Config:
    """Build a Config pointing at ws://localhost:{port}/ws/ with fast intervals.

    ``port=0`` is fine for unit tests that don't actually connect - the URL
    is only consulted by the websocket client, which the tests mock. A typed
    ``garage`` is round-tripped into the raw integrations table; extra raw
    sections (e.g. caddy) can be passed via ``integrations``.
    """
    integ: dict[str, dict[str, object]] = dict(integrations or {})
    if garage is not None:
        integ["garage"] = garage_raw_table(garage)
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
        metrics=MetricsConfig(
            push_interval_seconds=metrics_push_interval, collect_containers=False
        ),
        project=ProjectConfig(
            project_dir=Path("/opt/myapp"),
            compose_file=Path("/opt/myapp/docker-compose.yml"),
            docker_service_name="web",
        ),
        storage=StorageConfig(db_path=tmp_path / "test.db"),
        integrations=integ,
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
    )


def envelope(
    type: MessageType,
    payload: dict[str, Any],
    *,
    v: int = SPEAKS[-1],
    agent_id: str = AGENT_ID,
    id: str | None = None,
    ts: datetime | None = None,
) -> Envelope:
    """A hand-built Envelope at the spoken version; override only what a test asserts on."""
    return Envelope(
        v=v,
        type=type,
        id=id if id is not None else str(uuid.uuid4()),
        ts=ts if ts is not None else datetime.now(UTC),
        agent_id=agent_id,
        payload=payload,
    )


def signed(env: Envelope, secret: bytes = SECRET) -> Envelope:
    """*env* signed the way the control plane signs it, then parsed off the wire
    so it carries ``received``, exactly as every frame ``verify_envelope`` sees."""
    frame = env.to_dict()
    frame["payload"] = {**frame["payload"], "hmac": sign_envelope(frame, secret)}
    return Envelope.from_json(json.dumps(frame))


def signed_command_request(
    command: str = "git_pull",
    *,
    agent_id: str = AGENT_ID,
    nonce: str | None = None,
    ts: datetime | None = None,
    secret: bytes = SECRET,
    params: dict[str, Any] | None = None,
) -> Envelope:
    """A signed command.request envelope; override only what a test asserts on."""
    return signed(
        envelope(
            MessageType.COMMAND_REQUEST,
            {
                "command": command,
                "params": params or {},
                "nonce": nonce if nonce is not None else generate_nonce(),
            },
            agent_id=agent_id,
            ts=ts,
        ),
        secret,
    )


def signed_command_sequence(
    commands: list[str] | None = None,
    *,
    steps: list[dict[str, Any]] | None = None,
    stop_on_failure: bool = True,
    agent_id: str = AGENT_ID,
    nonce: str | None = None,
    ts: datetime | None = None,
    secret: bytes = SECRET,
    sequence_id: str | None = None,
) -> Envelope:
    """A signed command.sequence envelope; override only what a test asserts on.
    ``commands`` are names with no params; ``steps`` is the wire shape, verbatim."""
    if steps is None:
        names = commands if commands is not None else ["git_pull", "docker_logs"]
        steps = [{"command": name, "params": {}} for name in names]
    return signed(
        envelope(
            MessageType.COMMAND_SEQUENCE,
            {
                "sequence_id": sequence_id
                if sequence_id is not None
                else str(uuid.uuid4()),
                "steps": steps,
                "stop_on_failure": stop_on_failure,
                "nonce": nonce if nonce is not None else generate_nonce(),
            },
            agent_id=agent_id,
            ts=ts,
        ),
        secret,
    )


def sign_command_request(command: str = "git_pull", **kwargs: Any) -> str:
    """``signed_command_request`` as the JSON string the wire carries."""
    return signed_command_request(command, **kwargs).to_json()


def sign_command_sequence(commands: list[str], **kwargs: Any) -> str:
    """``signed_command_sequence`` as the JSON string the wire carries."""
    return signed_command_sequence(commands, **kwargs).to_json()


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
        failure_reason=FailureReason.EXIT_CODE,
    )
