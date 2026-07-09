"""Tests for stormpulse.protocol."""

from __future__ import annotations

import json
from dataclasses import asdict, fields
from datetime import UTC
from typing import Any

import pytest

from stormpulse.protocol import (
    CommandProgressPayload,
    CommandRequestPayload,
    CommandResultPayload,
    CommandSequencePayload,
    ContainerInfo,
    Envelope,
    LogBatchPayload,
    MessageType,
    MetricsPayload,
    ProtocolError,
    RegisterPayload,
    TransferStats,
    make_command_progress,
    make_command_result,
    make_heartbeat,
    make_log_batch,
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
            "commands": ["git_pull", "docker_logs"],
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
# Envelope parsing - happy path
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
    assert len(env.payload["commands"]) == 2
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
    assert rebuilt.payload["commands"] is None


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
    original = make_register("test-agent", "0.1.0", "tok-abc-123", commands=commands)
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
    assert len(payload.commands) == 2


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
    assert payload.commands is None


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
    data: dict[str, Any] = {
        "version": "0.2.0",
        "pulse_token": "tok-123",
        "commands": commands,
    }
    payload = RegisterPayload.from_dict(data)
    assert payload.commands == commands


def test_register_payload_from_dict_null_commands() -> None:
    data: dict[str, Any] = {
        "version": "0.2.0",
        "pulse_token": "tok-123",
        "commands": None,
    }
    payload = RegisterPayload.from_dict(data)
    assert payload.commands is None


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
        "rate_bytes_per_sec", "eta_seconds", "objects_current", "objects_total",
    }


def test_command_progress_payload_transfer_fields_default_absent() -> None:
    """Every non-transfer command emits progress without them. They are
    optional by construction (a default), never by convention."""
    payload = CommandProgressPayload(
        request_id="req-9", command="caddy_cert_status", group="caddy",
        stage="starting", current=0,
    )
    assert payload.rate_bytes_per_sec is None
    assert payload.eta_seconds is None
    assert payload.objects_current is None
    assert payload.objects_total is None


def test_command_progress_payload_from_an_older_agents_dict() -> None:
    """A payload dict predating the transfer fields still deserializes. This
    is why adding them needed no protocol version bump."""
    payload = CommandProgressPayload.from_dict({
        "request_id": "req-9", "command": "rclone_migrate", "group": "buckets",
        "stage": "running", "current": 1000, "total": 5000, "message": "x",
    })
    assert payload.rate_bytes_per_sec is None
    assert payload.eta_seconds is None


def test_command_progress_payload_ignores_unknown_keys() -> None:
    """Forward compatibility in the other direction: a newer peer may send
    fields this build has never heard of, and they are dropped, not fatal."""
    payload = CommandProgressPayload.from_dict({
        "request_id": "req-9", "command": "rclone_migrate", "group": "buckets",
        "stage": "running", "current": 1, "a_field_from_the_future": 42,
    })
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
    assert env.payload["commands"] is None
    Envelope.from_json(env.to_json())


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
    env = make_register("test-01", "0.1.0", "tok-abc-123", commands=commands)
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
        "test-01", metrics, job_load={"pending": 8, "running": 6},
    )
    assert env.payload["jobs"] == {"pending": 8, "running": 6}
    Envelope.from_json(env.to_json())


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


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_extra_envelope_fields_ignored(heartbeat_dict: dict[str, Any]) -> None:
    heartbeat_dict["extra"] = "ignored"
    env = Envelope.from_json(json.dumps(heartbeat_dict))
    assert env.type == MessageType.HEARTBEAT


def test_extra_payload_fields_ignored() -> None:
    data: dict[str, Any] = {
        "version": "0.1.0",
        "pulse_token": "tok",
        "extra": "ignored",
    }
    payload = RegisterPayload.from_dict(data)
    assert payload.version == "0.1.0"


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
        "agent-1",
        "0.3.0",
        "tok",
        log_groups=["storage", "pulse"],
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
        commands={"git_pull": {"group": "deploy"}},
        integrations=integrations,
        log_groups=["storage"],
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
    import uuid
    from datetime import datetime

    from stormpulse.protocol import Envelope

    original = Envelope(
        v=1,
        type=ack_type,
        id=str(uuid.uuid4()),
        ts=datetime.now(UTC).replace(microsecond=0),
        agent_id="agent-1",
        payload={"detail": "ok"},
    )
    rt = Envelope.from_json(original.to_json())
    assert rt.type is ack_type
    assert rt.payload == {"detail": "ok"}
    # Re-serialize: byte-for-byte stable
    assert rt.to_json() == original.to_json()


def test_envelope_post_init_rejects_unsupported_version() -> None:
    """Directly constructing with v!=1 must raise (not just from_json)."""
    from datetime import datetime

    from stormpulse.protocol import Envelope

    with pytest.raises(ProtocolError, match="Unsupported protocol version"):
        Envelope(
            v=2,
            type=MessageType.HEARTBEAT,
            id="x",
            ts=datetime.now(UTC),
            agent_id="a",
            payload={},
        )


def test_envelope_post_init_rejects_naive_timestamp() -> None:
    from datetime import datetime

    from stormpulse.protocol import Envelope

    with pytest.raises(ProtocolError, match="timezone-aware"):
        Envelope(
            v=1,
            type=MessageType.HEARTBEAT,
            id="x",
            ts=datetime(2026, 1, 1),  # naive
            agent_id="a",
            payload={},
        )
