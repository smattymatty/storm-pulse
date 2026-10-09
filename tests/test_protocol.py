"""Tests for stormpulse.protocol."""

from __future__ import annotations

import json
from dataclasses import asdict, fields
from datetime import UTC
from typing import Any

import pytest

from stormpulse.protocol import (
    MESSAGES,
    SPEAKS,
    CancelReason,
    CommandCancelledPayload,
    CommandProgressPayload,
    CommandRequestPayload,
    CommandResultPayload,
    CommandSequencePayload,
    Component,
    ContainerInfo,
    Envelope,
    FailureReason,
    LogBatchPayload,
    MessageType,
    MetricsPayload,
    ProtocolError,
    RegisterPayload,
    ResultError,
    Step,
    TransferStats,
    make_command_cancelled,
    make_command_progress,
    make_command_result,
    make_error,
    make_heartbeat,
    make_log_batch,
    make_metrics_push,
    make_register,
)
from tests.helpers import (
    FAKE_METRICS,
    envelope,
    register_fields,
    register_payload,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def heartbeat_dict() -> dict[str, Any]:
    return {
        "v": SPEAKS[-1],
        "type": "heartbeat",
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "ts": "2026-02-21T12:00:00Z",
        "agent_id": "vps-toronto-01",
        "payload": {},
    }


@pytest.fixture
def metrics_dict() -> dict[str, Any]:
    return {
        "v": SPEAKS[-1],
        "type": "metrics.push",
        "id": "550e8400-e29b-41d4-a716-446655440001",
        "ts": "2026-02-21T12:00:00Z",
        "agent_id": "vps-toronto-01",
        "payload": {
            "cpu_percent": 23.5,
            "memory_percent": 61.2,
            "memory_used_mb": 1245.0,
            "memory_total_mb": 2048.0,
            "disk_percent": 45.0,
            "disk_used_gb": 18.2,
            "disk_total_gb": 40.0,
            "load_avg_1m": 0.75,
            "load_avg_5m": 0.50,
            "uptime_seconds": 864000.0,
            "containers": [
                {"name": "web", "status": "running", "image": "myapp:latest"},
            ],
        },
    }


@pytest.fixture
def command_request_dict() -> dict[str, Any]:
    return {
        "v": SPEAKS[-1],
        "type": "command.request",
        "id": "550e8400-e29b-41d4-a716-446655440002",
        "ts": "2026-02-21T12:00:00Z",
        "agent_id": "vps-toronto-01",
        "payload": {
            "command": "git_pull",
            "params": {},
            "hmac": "abc123",
            "nonce": "nonce-1",
        },
    }


@pytest.fixture
def command_result_dict() -> dict[str, Any]:
    return {
        "v": SPEAKS[-1],
        "type": "command.result",
        "id": "550e8400-e29b-41d4-a716-446655440003",
        "ts": "2026-02-21T12:00:00Z",
        "agent_id": "vps-toronto-01",
        "payload": {
            "request_id": "550e8400-e29b-41d4-a716-446655440002",
            "command": "git_pull",
            "group": "deploy",
            "success": True,
            "exit_code": 0,
            "stdout": "Already up to date.\n",
            "stderr": "",
            "duration_ms": 342,
        },
    }


@pytest.fixture
def command_sequence_dict() -> dict[str, Any]:
    return {
        "v": SPEAKS[-1],
        "type": "command.sequence",
        "id": "550e8400-e29b-41d4-a716-446655440004",
        "ts": "2026-02-21T12:00:00Z",
        "agent_id": "vps-toronto-01",
        "payload": {
            "sequence_id": "seq-001",
            "steps": [
                {"command": "git_pull", "params": {}},
                {"command": "docker_logs", "params": {"tail": "100"}},
            ],
            "stop_on_failure": True,
            "hmac": "def456",
            "nonce": "nonce-2",
        },
    }


@pytest.fixture
def register_dict() -> dict[str, Any]:
    return {
        "v": SPEAKS[-1],
        "type": "register",
        "id": "550e8400-e29b-41d4-a716-446655440005",
        "ts": "2026-02-21T12:00:00Z",
        "agent_id": "vps-toronto-01",
        "payload": register_payload(),
    }


# ---------------------------------------------------------------------------
# Envelope parsing - happy path
# ---------------------------------------------------------------------------


def test_parse_heartbeat(heartbeat_dict: dict[str, Any]) -> None:
    env = Envelope.from_json(json.dumps(heartbeat_dict))
    assert env.type == MessageType.HEARTBEAT
    assert env.payload == {}
    assert env.agent_id == "vps-toronto-01"
    assert env.v == SPEAKS[-1]


def test_parse_from_bytes(heartbeat_dict: dict[str, Any]) -> None:
    raw = json.dumps(heartbeat_dict).encode("utf-8")
    env = Envelope.from_json(raw)
    assert env.type == MessageType.HEARTBEAT
    assert env.agent_id == "vps-toronto-01"


def test_parse_metrics_push(metrics_dict: dict[str, Any]) -> None:
    env = Envelope.from_json(json.dumps(metrics_dict))
    assert env.type == MessageType.METRICS_PUSH
    assert env.payload["cpu_percent"] == 23.5
    assert len(env.payload["containers"]) == 1


def test_parse_command_request(command_request_dict: dict[str, Any]) -> None:
    env = Envelope.from_json(json.dumps(command_request_dict))
    assert env.type == MessageType.COMMAND_REQUEST
    assert env.payload["command"] == "git_pull"


def test_parse_command_result(command_result_dict: dict[str, Any]) -> None:
    env = Envelope.from_json(json.dumps(command_result_dict))
    assert env.type == MessageType.COMMAND_RESULT
    assert env.payload["success"] is True
    assert env.payload["exit_code"] == 0


def test_parse_command_sequence(command_sequence_dict: dict[str, Any]) -> None:
    env = Envelope.from_json(json.dumps(command_sequence_dict))
    assert env.type == MessageType.COMMAND_SEQUENCE
    assert len(env.payload["steps"]) == 2
    assert env.payload["stop_on_failure"] is True


def test_parse_register(register_dict: dict[str, Any]) -> None:
    env = Envelope.from_json(json.dumps(register_dict))
    assert env.type == MessageType.REGISTER
    assert env.payload["version"] == "0.1.0"


def test_parse_timestamp_with_offset(heartbeat_dict: dict[str, Any]) -> None:
    heartbeat_dict["ts"] = "2026-02-21T12:00:00+05:30"
    env = Envelope.from_json(json.dumps(heartbeat_dict))
    assert env.ts.tzinfo is not None


# ---------------------------------------------------------------------------
# Envelope round-trip
# ---------------------------------------------------------------------------


def test_roundtrip_heartbeat() -> None:
    original = make_heartbeat("test-agent")
    rebuilt = Envelope.from_json(original.to_json())
    assert rebuilt.type == original.type
    assert rebuilt.agent_id == original.agent_id
    assert rebuilt.payload == original.payload


def test_roundtrip_register() -> None:
    original = make_register("test-agent", "0.1.0", "tok-abc-123", **register_fields())
    rebuilt = Envelope.from_json(original.to_json())
    assert rebuilt.payload["version"] == "0.1.0"
    assert rebuilt.payload["pulse_token"] == "tok-abc-123"
    assert rebuilt.payload["commands"] == {}
    assert RegisterPayload.from_dict(rebuilt.payload) == RegisterPayload.from_dict(
        original.payload
    )


def test_roundtrip_register_with_commands() -> None:
    commands = {
        "docker_up": {
            "group": "deploy",
            "description": "Start containers",
            "template": ["docker", "compose", "up", "-d"],
            "timeout": 120,
            "requires_confirmation": False,
            "params": {},
        },
        "git_pull": {
            "group": "deploy",
            "description": "Pull latest changes",
            "template": ["git", "-C", "{project_dir}", "pull"],
            "timeout": 60,
            "requires_confirmation": False,
            "params": {},
        },
    }
    original = make_register(
        "test-agent", "0.1.0", "tok-abc-123", **register_fields(commands=commands)
    )
    rebuilt = Envelope.from_json(original.to_json())
    assert rebuilt.payload["commands"] == commands


def test_roundtrip_metrics() -> None:
    metrics = MetricsPayload(
        cpu_percent=10.0,
        memory_percent=50.0,
        memory_used_mb=1024.0,
        memory_total_mb=2048.0,
        disk_percent=30.0,
        disk_used_gb=12.0,
        disk_total_gb=40.0,
        load_avg_1m=0.5,
        load_avg_5m=0.3,
        uptime_seconds=3600.0,
        containers=[ContainerInfo(name="web", status="running", image="app:1")],
    )
    original = make_metrics_push("test-agent", metrics)
    rebuilt = Envelope.from_json(original.to_json())
    assert rebuilt.payload["cpu_percent"] == 10.0
    assert rebuilt.payload["containers"][0]["name"] == "web"


def test_roundtrip_command_result() -> None:
    result = CommandResultPayload(
        request_id="req-1",
        command="git_pull",
        group="deploy",
        success=True,
        exit_code=0,
        stdout="ok\n",
        stderr="",
        duration_ms=100,
        sequence_id="seq-1",
    )
    original = make_command_result("test-agent", result)
    rebuilt = Envelope.from_json(original.to_json())
    assert rebuilt.payload["sequence_id"] == "seq-1"
    assert rebuilt.payload["success"] is True


def test_roundtrip_command_result_no_sequence_id() -> None:
    result = CommandResultPayload(
        request_id="req-2",
        command="git_pull",
        group="deploy",
        success=False,
        exit_code=1,
        stdout="",
        stderr="error\n",
        duration_ms=50,
    )
    original = make_command_result("test-agent", result)
    rebuilt = Envelope.from_json(original.to_json())
    assert rebuilt.payload["sequence_id"] is None


def test_roundtrip_command_result_with_failure_reason() -> None:
    result = CommandResultPayload(
        request_id="req-3",
        command="git_pull",
        group="deploy",
        success=False,
        exit_code=-1,
        stdout="",
        stderr="",
        duration_ms=10000,
        failure_reason=FailureReason.TIMEOUT,
    )
    original = make_command_result("test-agent", result)
    rebuilt = Envelope.from_json(original.to_json())
    assert rebuilt.payload["failure_reason"] == "timeout"
    payload = CommandResultPayload.from_dict(rebuilt.payload)
    assert payload.failure_reason is FailureReason.TIMEOUT


def test_command_result_lifts_a_member_string_and_refuses_a_stranger() -> None:
    """CORE-011 d9: ``failure_reason`` is the closed ``FailureReason`` set. The
    string off the wire becomes the member; a word outside the set is a bug."""
    lifted = CommandResultPayload.from_dict(
        {
            "request_id": "r",
            "command": "c",
            "group": "g",
            "success": False,
            "exit_code": 1,
            "stdout": "",
            "stderr": "",
            "duration_ms": 1,
            "failure_reason": "unknown_command",
        }
    )
    assert lifted.failure_reason is FailureReason.UNKNOWN_COMMAND
    with pytest.raises(ValueError, match="'typo' is not a FailureReason"):
        _result(success=False, exit_code=1, failure_reason="typo")
    for name in ("unknown_command", "forbidden", "cancelled", "integration_failed"):
        assert FailureReason(name).value == name


# ---------------------------------------------------------------------------
# Payload from_dict
# ---------------------------------------------------------------------------


def test_metrics_payload_from_dict(metrics_dict: dict[str, Any]) -> None:
    payload = MetricsPayload.from_dict(metrics_dict["payload"])
    assert payload.cpu_percent == 23.5
    assert len(payload.containers) == 1
    assert payload.containers[0].name == "web"


def test_command_request_payload_from_dict(
    command_request_dict: dict[str, Any],
) -> None:
    payload = CommandRequestPayload.from_dict(command_request_dict["payload"])
    assert payload.command == "git_pull"
    assert payload.nonce == "nonce-1"


def test_command_sequence_payload_from_dict(
    command_sequence_dict: dict[str, Any],
) -> None:
    payload = CommandSequencePayload.from_dict(command_sequence_dict["payload"])
    assert payload.sequence_id == "seq-001"
    assert payload.steps == [
        Step(command="git_pull", params={}),
        Step(command="docker_logs", params={"tail": "100"}),
    ]


def test_command_result_payload_from_dict(command_result_dict: dict[str, Any]) -> None:
    payload = CommandResultPayload.from_dict(command_result_dict["payload"])
    assert payload.success is True
    assert payload.sequence_id is None
    assert payload.failure_reason is None


def test_command_result_payload_with_sequence_id() -> None:
    data: dict[str, Any] = {
        "request_id": "r1",
        "command": "git_pull",
        "group": "deploy",
        "success": True,
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "duration_ms": 10,
        "sequence_id": "seq-99",
    }
    payload = CommandResultPayload.from_dict(data)
    assert payload.sequence_id == "seq-99"


def test_register_payload_from_dict(register_dict: dict[str, Any]) -> None:
    payload = RegisterPayload.from_dict(register_dict["payload"])
    assert payload.version == "0.1.0"
    assert payload.commands == {}
    assert payload.unsealed_since is None


def test_register_payload_from_dict_with_commands() -> None:
    commands = {
        "git_pull": {
            "group": "deploy",
            "description": "Pull latest",
            "template": ["git", "pull"],
            "timeout": 60,
            "requires_confirmation": False,
            "params": {},
        },
    }
    payload = RegisterPayload.from_dict(register_payload(commands=commands))
    assert payload.commands == commands


def test_register_payload_from_dict_builds_components() -> None:
    """``components`` is the one nested class under register; each item is typed."""
    raw = [{"name": "stormpulse", "version": "0.7.0", "digest": ""}]
    payload = RegisterPayload.from_dict(register_payload(components=raw))
    assert payload.components == [Component("stormpulse", "0.7.0", "")]
    with pytest.raises(ProtocolError, match="Component: missing fields"):
        RegisterPayload.from_dict(register_payload(components=[{"name": "x"}]))
    with pytest.raises(ProtocolError, match="components: expected list"):
        RegisterPayload.from_dict(register_payload(components={}))


def test_container_info_from_dict() -> None:
    info = ContainerInfo.from_dict(
        {"name": "db", "status": "exited", "image": "postgres:16"}
    )
    assert info.name == "db"


def test_payload_asdict_roundtrip() -> None:
    original = CommandResultPayload(
        request_id="r1",
        command="git_pull",
        group="deploy",
        success=True,
        exit_code=0,
        stdout="",
        stderr="",
        duration_ms=10,
    )
    d = asdict(original)
    rebuilt = CommandResultPayload.from_dict(d)
    assert rebuilt == original


def test_command_progress_payload_defaults() -> None:
    payload = CommandProgressPayload(
        request_id="req-9",
        command="garage_bucket_clear",
        group="garage",
        stage="starting",
        current=0,
    )
    assert payload.total is None
    assert payload.message == ""


def test_command_progress_payload_from_dict() -> None:
    data = {
        "request_id": "req-9",
        "command": "garage_bucket_clear",
        "group": "garage",
        "stage": "running",
        "current": 1000,
        "total": 5000,
        "message": "deleted batch 1",
    }
    payload = CommandProgressPayload.from_dict(data)
    assert payload.stage == "running"
    assert payload.current == 1000
    assert payload.total == 5000


def test_transfer_stats_fields_match_the_progress_payload() -> None:
    """A fitness function, not a nicety. ``_make_progress_callback`` flattens
    a TransferStats onto the payload with ``**asdict(transfer)``, which is
    only safe while every TransferStats field name is also a payload field
    name. Rename one without the other and this fails here, loudly, instead
    of raising TypeError inside a live transfer.
    """
    transfer_fields = {f.name for f in fields(TransferStats)}
    payload_fields = {f.name for f in fields(CommandProgressPayload)}
    assert transfer_fields <= payload_fields
    # And they are the four the wire contract declares.
    assert transfer_fields == {
        "rate_bytes_per_sec",
        "eta_seconds",
        "objects_current",
        "objects_total",
    }


def test_command_progress_payload_transfer_fields_default_absent() -> None:
    """Every non-transfer command emits progress without them. They are
    optional by construction (a default), never by convention."""
    payload = CommandProgressPayload(
        request_id="req-9",
        command="caddy_cert_status",
        group="caddy",
        stage="starting",
        current=0,
    )
    assert payload.rate_bytes_per_sec is None
    assert payload.eta_seconds is None
    assert payload.objects_current is None
    assert payload.objects_total is None


def test_command_progress_payload_from_an_older_agents_dict() -> None:
    """A payload dict predating the transfer fields still deserializes. This
    is why adding them needed no protocol version bump."""
    payload = CommandProgressPayload.from_dict(
        {
            "request_id": "req-9",
            "command": "rclone_migrate",
            "group": "buckets",
            "stage": "running",
            "current": 1000,
            "total": 5000,
            "message": "x",
        }
    )
    assert payload.rate_bytes_per_sec is None
    assert payload.eta_seconds is None


def test_command_progress_payload_ignores_unknown_keys() -> None:
    """Forward compatibility in the other direction: a newer peer may send
    fields this build has never heard of, and they are dropped, not fatal."""
    payload = CommandProgressPayload.from_dict(
        {
            "request_id": "req-9",
            "command": "rclone_migrate",
            "group": "buckets",
            "stage": "running",
            "current": 1,
            "a_field_from_the_future": 42,
        }
    )
    assert payload.current == 1


def test_command_progress_payload_missing_required_raises() -> None:
    with pytest.raises(ProtocolError):
        CommandProgressPayload.from_dict(
            {
                "request_id": "req-9",
                "command": "x",
                "group": "g",
                "stage": "starting",
            }
        )


def test_roundtrip_command_progress() -> None:
    progress = CommandProgressPayload(
        request_id="req-9",
        command="garage_bucket_clear",
        group="garage",
        stage="running",
        current=2000,
        total=5000,
        message="batch 2/5",
    )
    original = make_command_progress("test-agent", progress)
    rebuilt = Envelope.from_json(original.to_json())
    assert rebuilt.type == MessageType.COMMAND_PROGRESS
    assert rebuilt.payload["stage"] == "running"
    assert rebuilt.payload["total"] == 5000
    parsed = CommandProgressPayload.from_dict(rebuilt.payload)
    assert parsed == progress


def test_roundtrip_command_progress_unknown_total() -> None:
    progress = CommandProgressPayload(
        request_id="req-9",
        command="garage_bucket_clear",
        group="garage",
        stage="starting",
        current=0,
    )
    original = make_command_progress("test-agent", progress)
    rebuilt = Envelope.from_json(original.to_json())
    assert rebuilt.payload["total"] is None
    assert rebuilt.payload["message"] == ""


def test_metrics_payload_asdict_roundtrip() -> None:
    original = MetricsPayload(
        cpu_percent=1.0,
        memory_percent=2.0,
        memory_used_mb=3.0,
        memory_total_mb=4.0,
        disk_percent=5.0,
        disk_used_gb=6.0,
        disk_total_gb=7.0,
        load_avg_1m=0.1,
        load_avg_5m=0.2,
        uptime_seconds=8.0,
        containers=[ContainerInfo(name="x", status="y", image="z")],
    )
    d = asdict(original)
    rebuilt = MetricsPayload.from_dict(d)
    assert rebuilt == original


# ---------------------------------------------------------------------------
# Envelope validation failures
# ---------------------------------------------------------------------------


def test_invalid_json_raises() -> None:
    with pytest.raises(ProtocolError, match="Invalid JSON"):
        Envelope.from_json("not json{{{")


def test_non_object_json_raises() -> None:
    with pytest.raises(ProtocolError, match="Expected JSON object"):
        Envelope.from_json("[1, 2, 3]")


def test_missing_envelope_field_raises(heartbeat_dict: dict[str, Any]) -> None:
    del heartbeat_dict["type"]
    with pytest.raises(ProtocolError, match="Missing envelope fields"):
        Envelope.from_json(json.dumps(heartbeat_dict))


def test_missing_multiple_fields_raises(heartbeat_dict: dict[str, Any]) -> None:
    del heartbeat_dict["type"]
    del heartbeat_dict["v"]
    with pytest.raises(ProtocolError, match="Missing envelope fields"):
        Envelope.from_json(json.dumps(heartbeat_dict))


@pytest.mark.parametrize("v", [1, SPEAKS[-1] + 1, True, 2.0, "2"])
def test_unsupported_version_raises(heartbeat_dict: dict[str, Any], v: object) -> None:
    """A version outside SPEAKS, a v1 frame first, is refused with its reason."""
    heartbeat_dict["v"] = v
    with pytest.raises(ProtocolError, match="Unsupported protocol version") as exc:
        Envelope.from_json(json.dumps(heartbeat_dict))
    assert exc.value.reason == "unsupported_version"
    assert str(exc.value).endswith(f"this agent speaks {list(SPEAKS)}")


def test_unknown_message_type_raises(heartbeat_dict: dict[str, Any]) -> None:
    heartbeat_dict["type"] = "foo.bar"
    with pytest.raises(ProtocolError, match="Unknown message type"):
        Envelope.from_json(json.dumps(heartbeat_dict))


def test_invalid_timestamp_raises(heartbeat_dict: dict[str, Any]) -> None:
    heartbeat_dict["ts"] = "not-a-date"
    with pytest.raises(ProtocolError, match="Invalid timestamp"):
        Envelope.from_json(json.dumps(heartbeat_dict))


def test_naive_timestamp_raises(heartbeat_dict: dict[str, Any]) -> None:
    heartbeat_dict["ts"] = "2026-02-21T12:00:00"
    with pytest.raises(ProtocolError, match="timezone"):
        Envelope.from_json(json.dumps(heartbeat_dict))


def test_non_string_timestamp_raises(heartbeat_dict: dict[str, Any]) -> None:
    heartbeat_dict["ts"] = 12345
    with pytest.raises(ProtocolError, match="Timestamp must be a string"):
        Envelope.from_json(json.dumps(heartbeat_dict))


def test_empty_agent_id_raises(heartbeat_dict: dict[str, Any]) -> None:
    heartbeat_dict["agent_id"] = ""
    with pytest.raises(ProtocolError, match="non-empty string"):
        Envelope.from_json(json.dumps(heartbeat_dict))


def test_non_string_agent_id_raises(heartbeat_dict: dict[str, Any]) -> None:
    heartbeat_dict["agent_id"] = 42
    with pytest.raises(ProtocolError, match="non-empty string"):
        Envelope.from_json(json.dumps(heartbeat_dict))


def test_non_dict_payload_raises(heartbeat_dict: dict[str, Any]) -> None:
    heartbeat_dict["payload"] = "not a dict"
    with pytest.raises(ProtocolError, match="payload must be a dict"):
        Envelope.from_json(json.dumps(heartbeat_dict))


# ---------------------------------------------------------------------------
# Payload validation failures
# ---------------------------------------------------------------------------


def test_metrics_missing_field_raises() -> None:
    data: dict[str, Any] = {"cpu_percent": 1.0}
    with pytest.raises(ProtocolError, match="missing fields"):
        MetricsPayload.from_dict(data)


def test_metrics_containers_not_list_raises() -> None:
    data: dict[str, Any] = {
        "cpu_percent": 1.0,
        "memory_percent": 2.0,
        "memory_used_mb": 3.0,
        "memory_total_mb": 4.0,
        "disk_percent": 5.0,
        "disk_used_gb": 6.0,
        "disk_total_gb": 7.0,
        "load_avg_1m": 0.1,
        "load_avg_5m": 0.2,
        "uptime_seconds": 8.0,
        "containers": "not a list",
    }
    with pytest.raises(ProtocolError, match="expected list"):
        MetricsPayload.from_dict(data)


def test_container_info_not_dict_raises() -> None:
    with pytest.raises(ProtocolError, match="expected dict"):
        ContainerInfo.from_dict("not a dict")


def test_command_request_missing_command_raises() -> None:
    with pytest.raises(ProtocolError, match="missing fields"):
        CommandRequestPayload.from_dict({"params": {}, "hmac": "x", "nonce": "y"})


def test_register_names_every_missing_field() -> None:
    """CORE-011 d7: no register field has a default, so an empty dict is refused
    naming all fifteen, ``unsealed_since`` among them (nullable, never absent)."""
    with pytest.raises(ProtocolError, match="RegisterPayload: missing fields") as info:
        RegisterPayload.from_dict({})
    names = {f.name for f in fields(RegisterPayload)}
    assert len(names) == 15
    assert all(f"'{name}'" in str(info.value) for name in names)
    with pytest.raises(ProtocolError, match=r"missing fields: \{'unsealed_since'\}"):
        RegisterPayload.from_dict(
            {k: v for k, v in register_payload().items() if k != "unsealed_since"}
        )


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------


def test_make_heartbeat_valid() -> None:
    env = make_heartbeat("test-01")
    assert env.type == MessageType.HEARTBEAT
    assert env.agent_id == "test-01"
    assert env.payload == {}
    Envelope.from_json(env.to_json())  # must survive round-trip


def test_make_register_valid() -> None:
    env = make_register("test-01", "0.1.0", "tok-abc-123", **register_fields())
    assert env.type == MessageType.REGISTER
    assert env.payload["version"] == "0.1.0"
    assert env.payload["pulse_token"] == "tok-abc-123"
    assert env.payload["commands"] == {}
    assert env.payload["speaks"] == list(SPEAKS)
    Envelope.from_json(env.to_json())


def test_make_register_carries_every_field_and_no_default() -> None:
    """The wire shows what the factory was given, field for field; a caller
    cannot leave one out, and ``speaks`` is never a caller's."""
    given = register_fields(
        components=[
            Component("stormpulse", "0.7.0", ""),
            Component("garage", "2.4.0", ""),
        ],
        in_flight=["req-1"],
        boot_id="",
    )
    env = make_register("test-01", "0.1.0", "tok-abc-123", **given)
    assert env.payload == {
        "version": "0.1.0",
        "pulse_token": "tok-abc-123",
        "speaks": list(SPEAKS),
        **given,
        "components": [asdict(c) for c in given["components"]],
    }
    without = {k: v for k, v in given.items() if k != "in_flight"}
    with pytest.raises(TypeError, match="in_flight"):
        make_register("test-01", "0.1.0", "tok", **without)


def test_make_register_with_commands() -> None:
    commands = {
        "docker_up": {
            "group": "deploy",
            "description": "Start containers",
            "template": ["docker", "compose", "up"],
            "timeout": 120,
            "requires_confirmation": False,
            "params": {},
        },
    }
    env = make_register(
        "test-01", "0.1.0", "tok-abc-123", **register_fields(commands=commands)
    )
    assert env.payload["commands"] == commands
    Envelope.from_json(env.to_json())


def test_make_metrics_push_valid() -> None:
    metrics = MetricsPayload(
        cpu_percent=5.0,
        memory_percent=10.0,
        memory_used_mb=512.0,
        memory_total_mb=1024.0,
        disk_percent=20.0,
        disk_used_gb=8.0,
        disk_total_gb=40.0,
        load_avg_1m=0.0,
        load_avg_5m=0.0,
        uptime_seconds=100.0,
        containers=[],
    )
    env = make_metrics_push("test-01", metrics)
    assert env.type == MessageType.METRICS_PUSH
    assert env.payload["cpu_percent"] == 5.0
    assert "jobs" not in env.payload  # absent unless job_load is provided
    Envelope.from_json(env.to_json())


def test_make_metrics_push_carries_job_load() -> None:
    metrics = MetricsPayload(
        cpu_percent=5.0,
        memory_percent=10.0,
        memory_used_mb=512.0,
        memory_total_mb=1024.0,
        disk_percent=20.0,
        disk_used_gb=8.0,
        disk_total_gb=40.0,
        load_avg_1m=0.0,
        load_avg_5m=0.0,
        uptime_seconds=100.0,
        containers=[],
    )
    env = make_metrics_push(
        "test-01",
        metrics,
        job_load={"pending": 8, "running": 6},
    )
    assert env.payload["jobs"] == {"pending": 8, "running": 6}
    Envelope.from_json(env.to_json())


def test_metrics_payload_round_trips_the_jobs_it_carries() -> None:
    """``jobs`` is a field (CORE-011 d3), so the far end's ``from_dict`` keeps it."""
    metrics = MetricsPayload(
        cpu_percent=5.0,
        memory_percent=10.0,
        memory_used_mb=512.0,
        memory_total_mb=1024.0,
        disk_percent=20.0,
        disk_used_gb=8.0,
        disk_total_gb=40.0,
        load_avg_1m=0.0,
        load_avg_5m=0.0,
        uptime_seconds=100.0,
        containers=[],
    )
    env = make_metrics_push("test-01", metrics, job_load={"pending": 8, "running": 6})
    parsed = MetricsPayload.from_dict(Envelope.from_json(env.to_json()).payload)
    assert parsed.jobs == {"pending": 8, "running": 6}
    assert (
        MetricsPayload.from_dict(make_metrics_push("t", metrics).payload).jobs is None
    )


def test_make_command_result_valid() -> None:
    result = CommandResultPayload(
        request_id="r1",
        command="git_pull",
        group="deploy",
        success=True,
        exit_code=0,
        stdout="up\n",
        stderr="",
        duration_ms=200,
    )
    env = make_command_result("test-01", result)
    assert env.type == MessageType.COMMAND_RESULT
    Envelope.from_json(env.to_json())


def _result(**overrides: Any) -> CommandResultPayload:
    base: dict[str, Any] = dict(
        request_id="r1",
        command="garage_bucket_clear",
        group="buckets",
        success=True,
        exit_code=0,
        stdout="",
        stderr="",
        duration_ms=5,
    )
    return CommandResultPayload(**(base | overrides))


def test_command_result_carries_the_figures_under_result_only() -> None:
    """CORE-011 d9: a job's extras ride under ``result``, never the top level,
    so the clobber mutant (an extra named ``success``) changes nothing standard."""
    payload = Envelope.from_json(
        make_command_result(
            "t", _result(result={"deleted_count": 3, "success": False})
        ).to_json()
    ).payload
    assert payload["result"] == {"deleted_count": 3, "success": False}
    assert payload["success"] is True
    assert "deleted_count" not in payload
    assert payload["error"] is None
    assert make_command_result("t", _result()).payload["result"] is None


def test_command_result_failure_derives_its_error_from_reason_and_stderr() -> None:
    failed = _result(
        success=False,
        exit_code=-1,
        stderr="UpdateBucket quota failed: 503",
        failure_reason=FailureReason.OS_ERROR,
    )
    payload = Envelope.from_json(make_command_result("t", failed).to_json()).payload
    assert payload["error"] == {
        "code": "os_error",
        "http_status": None,
        "upstream_code": None,
        "message": "UpdateBucket quota failed: 503",
    }
    rebuilt = CommandResultPayload.from_dict(payload)
    assert rebuilt.error == ResultError(
        "os_error", None, None, "UpdateBucket quota failed: 503"
    )
    # A failure with no reason still names one; a parsed error wins over derivation.
    bare = make_command_result("t", _result(success=False, exit_code=1)).payload
    assert bare["error"]["code"] == "unspecified"
    parsed = ResultError("bucket_delete_failed", 404, "NoSuchBucket", "gone")
    finer = _result(success=False, failure_reason=FailureReason.OS_ERROR, error=parsed)
    assert make_command_result("t", finer).payload["error"] == asdict(parsed)


def test_command_result_refuses_an_error_on_a_success() -> None:
    with pytest.raises(ValueError, match="cannot succeed with an error"):
        _result(error=ResultError("x", None, None, ""))


def test_command_result_from_dict_types_the_nested_error() -> None:
    data = asdict(_result(success=False))
    data["error"] = {
        "code": "timeout",
        "http_status": None,
        "upstream_code": None,
        "message": "slow",
    }
    assert CommandResultPayload.from_dict(data).error == ResultError(
        "timeout", None, None, "slow"
    )
    data["error"] = "timeout"
    with pytest.raises(ProtocolError, match="ResultError: expected dict"):
        CommandResultPayload.from_dict(data)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_extra_envelope_fields_ignored(heartbeat_dict: dict[str, Any]) -> None:
    heartbeat_dict["extra"] = "ignored"
    env = Envelope.from_json(json.dumps(heartbeat_dict))
    assert env.type == MessageType.HEARTBEAT


def test_extra_payload_fields_ignored() -> None:
    payload = RegisterPayload.from_dict(register_payload(extra="ignored"))
    assert payload.version == "0.1.0"
    assert not hasattr(payload, "extra")


def test_empty_containers_valid() -> None:
    data: dict[str, Any] = {
        "cpu_percent": 1.0,
        "memory_percent": 2.0,
        "memory_used_mb": 3.0,
        "memory_total_mb": 4.0,
        "disk_percent": 5.0,
        "disk_used_gb": 6.0,
        "disk_total_gb": 7.0,
        "load_avg_1m": 0.1,
        "load_avg_5m": 0.2,
        "uptime_seconds": 8.0,
        "containers": [],
    }
    payload = MetricsPayload.from_dict(data)
    assert payload.containers == []


def test_message_type_enum_values() -> None:
    assert MessageType.HEARTBEAT.value == "heartbeat"
    assert MessageType.METRICS_PUSH.value == "metrics.push"
    assert MessageType.COMMAND_REQUEST.value == "command.request"
    assert MessageType.COMMAND_RESULT.value == "command.result"
    assert MessageType.COMMAND_SEQUENCE.value == "command.sequence"
    assert MessageType.REGISTER.value == "register"
    assert MessageType.REGISTER_OK.value == "register.ok"
    assert MessageType.HEARTBEAT_ACK.value == "heartbeat.ack"
    assert MessageType.METRICS_ACK.value == "metrics.ack"
    assert MessageType.COMMAND_RESULT_ACK.value == "command.result.ack"
    assert MessageType.ERROR.value == "error"


def test_make_error_carries_reason_and_type_only() -> None:
    """The error reply is a reason code and the refused type: no payload, no raw frame."""
    env = make_error("test-01", "unknown_type", "command.undo")
    assert env.type is MessageType.ERROR
    assert env.v == SPEAKS[-1]
    assert env.payload == {"error": "unknown_type", "type": "command.undo"}
    assert make_error("test-01", "bad_envelope", None).payload["type"] is None


def test_unknown_type_error_quotes_at_most_64_chars_of_the_type() -> None:
    """A refused frame's ``type`` rides on the exception, capped; a non-string type is None."""
    frame = make_heartbeat("test-01").to_dict()
    frame["type"] = "z" * 100
    with pytest.raises(ProtocolError) as exc_info:
        Envelope.from_json(json.dumps(frame))
    assert exc_info.value.reason == "unknown_type"
    assert exc_info.value.msg_type == "z" * 64
    frame["type"] = 7
    with pytest.raises(ProtocolError) as exc_info:
        Envelope.from_json(json.dumps(frame))
    assert exc_info.value.msg_type is None
    assert ProtocolError("x").msg_type is None


def test_to_json_uses_compact_separators() -> None:
    env = make_heartbeat("test-01")
    raw = env.to_json()
    expected = json.dumps(json.loads(raw), separators=(",", ":"))
    assert raw == expected


def test_timestamp_z_suffix() -> None:
    env = make_heartbeat("test-01")
    raw = env.to_json()
    assert "+00:00" not in raw
    data = json.loads(raw)
    assert data["ts"].endswith("Z")


def test_to_dict_returns_plain_dict() -> None:
    env = make_heartbeat("test-01")
    d = env.to_dict()
    assert isinstance(d, dict)
    assert d["type"] == "heartbeat"
    assert isinstance(d["ts"], str)


def test_envelope_immutable() -> None:
    env = make_heartbeat("test-01")
    with pytest.raises(AttributeError):
        env.agent_id = "changed"  # type: ignore[misc]


def test_payload_immutable() -> None:
    payload = RegisterPayload.from_dict(register_payload())
    with pytest.raises(AttributeError):
        payload.version = "0.2.0"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# LOG_BATCH / LogBatchPayload
# ---------------------------------------------------------------------------


def test_log_batch_payload_roundtrip() -> None:
    original = LogBatchPayload(
        group="storage",
        parser="garage_s3",
        batch_id="abc-123",
        lines=[{"ts": "2026-04-10T13:00:00Z", "bucket": "b1"}],
        dropped=2,
        from_position=100,
        to_position=500,
    )
    restored = LogBatchPayload.from_dict(asdict(original))
    assert restored == original


def test_log_batch_payload_missing_field_raises() -> None:
    with pytest.raises(ProtocolError):
        LogBatchPayload.from_dict({"group": "storage"})


def test_make_log_batch_envelope() -> None:
    env = make_log_batch(
        "agent-1",
        group="storage",
        parser="garage_s3",
        batch_id="b1",
        lines=[{"ts": "x", "bucket": "b"}],
        dropped=0,
        from_position=0,
        to_position=100,
    )
    assert env.type == MessageType.LOG_BATCH
    assert env.agent_id == "agent-1"
    assert env.payload["group"] == "storage"
    assert env.payload["batch_id"] == "b1"
    assert len(env.payload["lines"]) == 1


def test_log_batch_envelope_json_roundtrip() -> None:
    env = make_log_batch(
        "a",
        group="pulse",
        parser="stormpulse",
        batch_id="bid",
        lines=[
            {"ts": "t", "level": "INFO", "message": "m", "event_type": "connection"}
        ],
        dropped=0,
        from_position=0,
        to_position=10,
    )
    raw = env.to_json()
    restored = Envelope.from_json(raw)
    assert restored.type == MessageType.LOG_BATCH
    payload = LogBatchPayload.from_dict(restored.payload)
    assert payload.batch_id == "bid"


def test_log_batch_ack_message_type_valid() -> None:
    assert MessageType("log.batch.ack") is MessageType.LOG_BATCH_ACK


def test_register_with_log_groups() -> None:
    env = make_register(
        "agent-1", "0.3.0", "tok", **register_fields(log_groups=["storage", "pulse"])
    )
    assert env.payload["log_groups"] == ["storage", "pulse"]


# ---------------------------------------------------------------------------
# Gap coverage - garage merging, ack round-trips, version post_init,
# LogBatch.lines invalid items
# ---------------------------------------------------------------------------


def test_make_metrics_push_with_integrations_merges_dict() -> None:
    from stormpulse.protocol import MetricsPayload

    metrics = MetricsPayload(
        cpu_percent=1.0,
        memory_percent=2.0,
        memory_used_mb=3.0,
        memory_total_mb=4.0,
        disk_percent=5.0,
        disk_used_gb=6.0,
        disk_total_gb=7.0,
        load_avg_1m=0.0,
        load_avg_5m=0.0,
        uptime_seconds=1.0,
        containers=[],
    )
    integrations = {
        "garage": {
            "status": "live",
            "disabled_reason": None,
            "state": {"node_id": "abc", "healthy": True, "buckets": []},
        }
    }
    env = make_metrics_push("agent-1", metrics, integrations=integrations)
    assert env.payload["integrations"] == integrations
    assert "garage" not in env.payload
    # Full round-trip preserves integrations
    from stormpulse.protocol import Envelope

    round_tripped = Envelope.from_json(env.to_json())
    assert round_tripped.payload["integrations"] == integrations
    assert round_tripped.payload["cpu_percent"] == 1.0


def test_make_register_with_integrations_and_log_groups() -> None:
    from stormpulse.protocol import Envelope

    integrations = {
        "garage": {
            "status": "live",
            "disabled_reason": None,
            "state": {"node_id": "n", "buckets": []},
        }
    }
    env = make_register(
        "agent-1",
        "1.0.0",
        "tok",
        **register_fields(
            commands={"git_pull": {"group": "deploy"}},
            integrations=integrations,
            log_groups=["storage"],
        ),
    )
    rt = Envelope.from_json(env.to_json())
    assert rt.payload["integrations"] == integrations
    assert "garage" not in rt.payload
    assert rt.payload["log_groups"] == ["storage"]
    assert rt.payload["commands"] == {"git_pull": {"group": "deploy"}}


@pytest.mark.parametrize(
    "ack_type",
    [
        MessageType.REGISTER_OK,
        MessageType.HEARTBEAT_ACK,
        MessageType.METRICS_ACK,
        MessageType.COMMAND_RESULT_ACK,
        MessageType.LOG_BATCH_ACK,
        MessageType.ERROR,
    ],
)
def test_ack_envelope_round_trip(ack_type: MessageType) -> None:
    """Dashboard-origin ack messages must round-trip through from_json/to_json unchanged."""
    from datetime import datetime

    original = envelope(
        ack_type,
        {"detail": "ok"},
        agent_id="agent-1",
        ts=datetime.now(UTC).replace(microsecond=0),
    )
    rt = Envelope.from_json(original.to_json())
    assert rt.type is ack_type
    assert rt.payload == {"detail": "ok"}
    # Re-serialize: byte-for-byte stable
    assert rt.to_json() == original.to_json()


def test_envelope_post_init_rejects_unsupported_version() -> None:
    """Directly constructing with a version outside SPEAKS must raise (not just from_json)."""
    with pytest.raises(ProtocolError, match="Unsupported protocol version") as exc:
        envelope(MessageType.HEARTBEAT, {}, v=SPEAKS[-1] + 1, id="x", agent_id="a")
    assert exc.value.reason == "unsupported_version"


def test_envelope_post_init_rejects_naive_timestamp() -> None:
    from datetime import datetime

    with pytest.raises(ProtocolError, match="timezone-aware"):
        envelope(
            MessageType.HEARTBEAT, {}, id="x", agent_id="a", ts=datetime(2026, 1, 1)
        )  # naive


# ---------------------------------------------------------------------------
# The message catalogue (CORE-011 d3): every type, its direction, its payload
# ---------------------------------------------------------------------------


def test_every_message_type_is_catalogued() -> None:
    from dataclasses import is_dataclass

    from stormpulse.protocol import MESSAGES, WirePayload

    assert set(MESSAGES) == set(MessageType)
    assert all(
        is_dataclass(row.payload) and issubclass(row.payload, WirePayload)
        for row in MESSAGES.values()
    )


def test_builders_emit_the_payload_class_the_catalogue_names() -> None:
    from stormpulse.protocol import MESSAGES, EventsBatchPayload, make_events_batch

    assert MESSAGES[MessageType.EVENTS_BATCH].payload is EventsBatchPayload
    batch = make_events_batch("test-01", batch_id="b1", events=[{"kind": "x"}])
    assert batch.payload == {"batch_id": "b1", "events": [{"kind": "x"}]}
    assert {f.name for f in fields(EventsBatchPayload)} == set(batch.payload)


def test_from_dict_rebuilds_integration_reports_from_a_mapping() -> None:
    """``integrations`` nests ``IntegrationReport`` under a dict, not a list: the
    parser follows the annotation's container, so a register or a metrics push
    reads back as the class the artifact declares, and a null stays null."""
    from stormpulse.protocol import IntegrationReport

    report = {
        "status": "live",
        "disabled_reason": None,
        "state": {"node_id": "n"},
        "command_specs_digest": "sha256:" + "ab" * 32,
        "commands_fenced": None,
        "generation": 3,
        "complete": False,
        "last_full_read_at": "2026-10-09T00:00:00Z",
        "changed_ids": ["b1"],
    }
    register = RegisterPayload.from_dict(
        register_payload(integrations={"garage": report})
    )
    assert register.integrations == {"garage": IntegrationReport(**report)}  # type: ignore[arg-type]
    assert asdict(register)["integrations"] == {"garage": report}
    metrics = MetricsPayload.from_dict({**asdict(FAKE_METRICS), "integrations": None})
    assert metrics.integrations is None
    with pytest.raises(ProtocolError, match="integrations: expected dict"):
        RegisterPayload.from_dict(register_payload(integrations=[report]))


def test_events_batch_nack_is_inbound_and_needs_batch_id_and_reason() -> None:
    """CORE-011 d6: the nack rides with its reason; a nack naming no batch is refused."""
    from stormpulse.protocol import MESSAGES, Direction, EventsBatchNackPayload

    spec = MESSAGES[MessageType.EVENTS_BATCH_NACK]
    assert spec.direction is Direction.DASHBOARD_TO_AGENT
    assert spec.payload is EventsBatchNackPayload
    nack = EventsBatchNackPayload.from_dict({"batch_id": "b1", "reason": "bad"})
    assert (nack.batch_id, nack.reason) == ("b1", "bad")
    with pytest.raises(ProtocolError, match="missing fields"):
        EventsBatchNackPayload.from_dict({"batch_id": "b1"})


# -- cancelled and steps (CORE-011 d9) --------------------------------------


def test_command_sequence_steps_are_typed_and_the_wrong_shape_is_refused() -> None:
    """A step is ``Step(command, params)`` built from its dict; the names-only
    list the sequence used to carry, a step with no params and a step object
    where the list should be are each refused at the seam."""
    base = {"sequence_id": "s", "stop_on_failure": True, "hmac": "h", "nonce": "n"}
    payload = CommandSequencePayload.from_dict(
        {**base, "steps": [{"command": "git_pull", "params": {"depth": "1"}}]}
    )
    assert payload.steps == [Step(command="git_pull", params={"depth": "1"})]
    with pytest.raises(ProtocolError, match="Step: expected dict, got str"):
        CommandSequencePayload.from_dict({**base, "steps": ["git_pull"]})
    with pytest.raises(ProtocolError, match="Step: missing fields"):
        CommandSequencePayload.from_dict({**base, "steps": [{"command": "git_pull"}]})
    with pytest.raises(ProtocolError, match="steps: expected list, got dict"):
        CommandSequencePayload.from_dict({**base, "steps": {"command": "git_pull"}})


def test_command_cancelled_is_outbound_and_names_the_job_and_the_reason() -> None:
    """One frame per job that will send no result: its id, its command and why."""
    from stormpulse.protocol import Direction

    spec = MESSAGES[MessageType.COMMAND_CANCELLED]
    assert spec.direction is Direction.AGENT_TO_DASHBOARD
    assert spec.payload is CommandCancelledPayload
    env = make_command_cancelled(
        "a1",
        request_id="r1",
        command="garage_bucket_clear",
        reason=CancelReason.SHUTDOWN,
    )
    assert env.type is MessageType.COMMAND_CANCELLED
    assert Envelope.from_json(env.to_json()).payload == {
        "request_id": "r1",
        "command": "garage_bucket_clear",
        "reason": "shutdown",
    }
    assert {r.value for r in CancelReason} == {"shutdown", "operator"}


def test_step_index_rides_a_result_and_is_null_outside_a_sequence(
    command_result_dict: dict[str, Any],
) -> None:
    single = CommandResultPayload.from_dict(command_result_dict["payload"])
    assert single.step_index is None
    assert make_command_result("a1", single).payload["step_index"] is None
    step = CommandResultPayload.from_dict(
        {**command_result_dict["payload"], "sequence_id": "s", "step_index": 1}
    )
    sent = make_command_result("a1", step).payload
    assert (sent["sequence_id"], sent["step_index"]) == ("s", 1)
