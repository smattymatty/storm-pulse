"""Tests for stormpulse.protocol."""

from __future__ import annotations

import json
from typing import Any

import pytest
from dataclasses import asdict

from stormpulse.protocol import (
    CommandRequestPayload,
    CommandResultPayload,
    CommandSequencePayload,
    ContainerInfo,
    Envelope,
    MessageType,
    MetricsPayload,
    ProtocolError,
    RegisterPayload,
    make_command_result,
    make_heartbeat,
    make_metrics_push,
    make_register,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def heartbeat_dict() -> dict[str, Any]:
    return {
        "v": 1,
        "type": "heartbeat",
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "ts": "2026-02-21T12:00:00Z",
        "agent_id": "vps-toronto-01",
        "payload": {},
    }


@pytest.fixture
def metrics_dict() -> dict[str, Any]:
    return {
        "v": 1,
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
        "v": 1,
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
        "v": 1,
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
        "v": 1,
        "type": "command.sequence",
        "id": "550e8400-e29b-41d4-a716-446655440004",
        "ts": "2026-02-21T12:00:00Z",
        "agent_id": "vps-toronto-01",
        "payload": {
            "sequence_id": "seq-001",
            "commands": ["git_pull", "docker_build", "docker_down", "docker_up", "django_migrate"],
            "stop_on_failure": True,
            "hmac": "def456",
            "nonce": "nonce-2",
        },
    }


@pytest.fixture
def register_dict() -> dict[str, Any]:
    return {
        "v": 1,
        "type": "register",
        "id": "550e8400-e29b-41d4-a716-446655440005",
        "ts": "2026-02-21T12:00:00Z",
        "agent_id": "vps-toronto-01",
        "payload": {"version": "0.1.0", "pulse_token": "tok-abc-123"},
    }


# ---------------------------------------------------------------------------
# Envelope parsing — happy path
# ---------------------------------------------------------------------------


def test_parse_heartbeat(heartbeat_dict: dict[str, Any]) -> None:
    env = Envelope.from_json(json.dumps(heartbeat_dict))
    assert env.type == MessageType.HEARTBEAT
    assert env.payload == {}
    assert env.agent_id == "vps-toronto-01"
    assert env.v == 1


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
    assert len(env.payload["commands"]) == 5
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
    original = make_register("test-agent", "0.1.0", "tok-abc-123")
    rebuilt = Envelope.from_json(original.to_json())
    assert rebuilt.payload["version"] == "0.1.0"
    assert rebuilt.payload["pulse_token"] == "tok-abc-123"


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
        command="docker_up",
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
        command="docker_build",
        group="deploy",
        success=False,
        exit_code=-1,
        stdout="",
        stderr="",
        duration_ms=10000,
        failure_reason="timeout",
    )
    original = make_command_result("test-agent", result)
    rebuilt = Envelope.from_json(original.to_json())
    assert rebuilt.payload["failure_reason"] == "timeout"
    payload = CommandResultPayload.from_dict(rebuilt.payload)
    assert payload.failure_reason == "timeout"


# ---------------------------------------------------------------------------
# Payload from_dict
# ---------------------------------------------------------------------------


def test_metrics_payload_from_dict(metrics_dict: dict[str, Any]) -> None:
    payload = MetricsPayload.from_dict(metrics_dict["payload"])
    assert payload.cpu_percent == 23.5
    assert len(payload.containers) == 1
    assert payload.containers[0].name == "web"


def test_command_request_payload_from_dict(command_request_dict: dict[str, Any]) -> None:
    payload = CommandRequestPayload.from_dict(command_request_dict["payload"])
    assert payload.command == "git_pull"
    assert payload.nonce == "nonce-1"


def test_command_sequence_payload_from_dict(command_sequence_dict: dict[str, Any]) -> None:
    payload = CommandSequencePayload.from_dict(command_sequence_dict["payload"])
    assert payload.sequence_id == "seq-001"
    assert len(payload.commands) == 5


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


def test_container_info_from_dict() -> None:
    info = ContainerInfo.from_dict({"name": "db", "status": "exited", "image": "postgres:16"})
    assert info.name == "db"


def test_payload_asdict_roundtrip() -> None:
    original = CommandResultPayload(
        request_id="r1", command="git_pull", group="deploy",
        success=True, exit_code=0, stdout="", stderr="", duration_ms=10,
    )
    d = asdict(original)
    rebuilt = CommandResultPayload.from_dict(d)
    assert rebuilt == original


def test_metrics_payload_asdict_roundtrip() -> None:
    original = MetricsPayload(
        cpu_percent=1.0, memory_percent=2.0, memory_used_mb=3.0,
        memory_total_mb=4.0, disk_percent=5.0, disk_used_gb=6.0,
        disk_total_gb=7.0, load_avg_1m=0.1, load_avg_5m=0.2,
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


def test_unsupported_version_raises(heartbeat_dict: dict[str, Any]) -> None:
    heartbeat_dict["v"] = 99
    with pytest.raises(ProtocolError, match="Unsupported protocol version"):
        Envelope.from_json(json.dumps(heartbeat_dict))


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
        "cpu_percent": 1.0, "memory_percent": 2.0, "memory_used_mb": 3.0,
        "memory_total_mb": 4.0, "disk_percent": 5.0, "disk_used_gb": 6.0,
        "disk_total_gb": 7.0, "load_avg_1m": 0.1, "load_avg_5m": 0.2,
        "uptime_seconds": 8.0, "containers": "not a list",
    }
    with pytest.raises(ProtocolError, match="expected list"):
        MetricsPayload.from_dict(data)


def test_container_info_not_dict_raises() -> None:
    with pytest.raises(ProtocolError, match="expected dict"):
        ContainerInfo.from_dict("not a dict")


def test_command_request_missing_command_raises() -> None:
    with pytest.raises(ProtocolError, match="missing fields"):
        CommandRequestPayload.from_dict({"params": {}, "hmac": "x", "nonce": "y"})


def test_register_missing_version_raises() -> None:
    with pytest.raises(ProtocolError, match="missing fields"):
        RegisterPayload.from_dict({})


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
    env = make_register("test-01", "0.1.0", "tok-abc-123")
    assert env.type == MessageType.REGISTER
    assert env.payload["version"] == "0.1.0"
    assert env.payload["pulse_token"] == "tok-abc-123"
    Envelope.from_json(env.to_json())


def test_make_metrics_push_valid() -> None:
    metrics = MetricsPayload(
        cpu_percent=5.0, memory_percent=10.0, memory_used_mb=512.0,
        memory_total_mb=1024.0, disk_percent=20.0, disk_used_gb=8.0,
        disk_total_gb=40.0, load_avg_1m=0.0, load_avg_5m=0.0,
        uptime_seconds=100.0, containers=[],
    )
    env = make_metrics_push("test-01", metrics)
    assert env.type == MessageType.METRICS_PUSH
    assert env.payload["cpu_percent"] == 5.0
    Envelope.from_json(env.to_json())


def test_make_command_result_valid() -> None:
    result = CommandResultPayload(
        request_id="r1", command="docker_up", group="deploy",
        success=True, exit_code=0, stdout="up\n", stderr="", duration_ms=200,
    )
    env = make_command_result("test-01", result)
    assert env.type == MessageType.COMMAND_RESULT
    Envelope.from_json(env.to_json())


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_extra_envelope_fields_ignored(heartbeat_dict: dict[str, Any]) -> None:
    heartbeat_dict["extra"] = "ignored"
    env = Envelope.from_json(json.dumps(heartbeat_dict))
    assert env.type == MessageType.HEARTBEAT


def test_extra_payload_fields_ignored() -> None:
    data: dict[str, Any] = {"version": "0.1.0", "pulse_token": "tok", "extra": "ignored"}
    payload = RegisterPayload.from_dict(data)
    assert payload.version == "0.1.0"


def test_empty_containers_valid() -> None:
    data: dict[str, Any] = {
        "cpu_percent": 1.0, "memory_percent": 2.0, "memory_used_mb": 3.0,
        "memory_total_mb": 4.0, "disk_percent": 5.0, "disk_used_gb": 6.0,
        "disk_total_gb": 7.0, "load_avg_1m": 0.1, "load_avg_5m": 0.2,
        "uptime_seconds": 8.0, "containers": [],
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
    payload = RegisterPayload(version="0.1.0", pulse_token="tok")
    with pytest.raises(AttributeError):
        payload.version = "0.2.0"  # type: ignore[misc]
