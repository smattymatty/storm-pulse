# skylos: ignore[SKY-Q502] one test module per source module; the envelope has this many seams
"""Tests for ``stormpulse.protocol.envelope``: parsing a frame, refusing a bad
one, serializing, and the outbound factories (``make`` plus the three that keep
logic).
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest

from stormpulse.protocol import (
    MESSAGES,
    SPEAKS,
    AckPayload,
    CommandProgressPayload,
    CommandResultPayload,
    Component,
    ContainerInfo,
    Direction,
    Envelope,
    FailureReason,
    HeartbeatPayload,
    IntegrationReport,
    LogBatchPayload,
    MessageType,
    MetricsPayload,
    ProtocolError,
    RegisterPayload,
    WirePayload,
    make,
    make_error,
    make_metrics_push,
    make_register,
)
from tests.helpers import (
    envelope,
    register_fields,
)

# -- parsing, happy path -----------------------------------------------------


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


def test_extra_envelope_fields_ignored(heartbeat_dict: dict[str, Any]) -> None:
    heartbeat_dict["extra"] = "ignored"
    env = Envelope.from_json(json.dumps(heartbeat_dict))
    assert env.type == MessageType.HEARTBEAT


# -- validation failures -----------------------------------------------------


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


def test_unknown_type_error_quotes_at_most_64_chars_of_the_type() -> None:
    """A refused frame's ``type`` rides on the exception, capped; a non-string type is None."""
    frame = make("test-01", HeartbeatPayload()).to_dict()
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


@pytest.mark.parametrize("key", ["id", "agent_id"])
@pytest.mark.parametrize("value", ["", 42])
def test_envelope_id_refusal_names_the_field(
    heartbeat_dict: dict[str, Any], key: str, value: object
) -> None:
    """``id`` and ``agent_id`` share one check; the refusal says which one failed."""
    heartbeat_dict[key] = value
    with pytest.raises(ProtocolError) as exc_info:
        Envelope.from_json(json.dumps(heartbeat_dict))
    assert str(exc_info.value) == f"{key} must be a non-empty string"


def test_non_dict_payload_raises(heartbeat_dict: dict[str, Any]) -> None:
    heartbeat_dict["payload"] = "not a dict"
    with pytest.raises(ProtocolError, match="payload must be a dict"):
        Envelope.from_json(json.dumps(heartbeat_dict))


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


# -- serialization -----------------------------------------------------------


def test_to_json_uses_compact_separators() -> None:
    env = make("test-01", HeartbeatPayload())
    raw = env.to_json()
    expected = json.dumps(json.loads(raw), separators=(",", ":"))
    assert raw == expected


def test_timestamp_z_suffix() -> None:
    env = make("test-01", HeartbeatPayload())
    raw = env.to_json()
    assert "+00:00" not in raw
    data = json.loads(raw)
    assert data["ts"].endswith("Z")


def test_to_dict_returns_plain_dict() -> None:
    env = make("test-01", HeartbeatPayload())
    d = env.to_dict()
    assert isinstance(d, dict)
    assert d["type"] == "heartbeat"
    assert isinstance(d["ts"], str)


def test_envelope_immutable() -> None:
    env = make("test-01", HeartbeatPayload())
    with pytest.raises(AttributeError):
        env.agent_id = "changed"  # type: ignore[misc]


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


# -- make, the outbound factory ----------------------------------------------


def test_make_heartbeat_valid() -> None:
    env = make("test-01", HeartbeatPayload())
    assert env.type == MessageType.HEARTBEAT
    assert env.agent_id == "test-01"
    assert env.payload == {}
    Envelope.from_json(env.to_json())  # must survive round-trip


_OUTBOUND_ROWS = [
    (msg_type, spec.payload)
    for msg_type, spec in MESSAGES.items()
    if spec.direction is not Direction.DASHBOARD_TO_AGENT
    and spec.payload is not MetricsPayload
]


@pytest.mark.parametrize(
    ("msg_type", "cls"), _OUTBOUND_ROWS, ids=lambda v: getattr(v, "value", None)
)
def test_make_resolves_the_catalogue_type_from_the_payload_class(
    msg_type: MessageType, cls: type[WirePayload], populated: Callable[[Any], Any]
) -> None:
    """One factory for every frame this side sends: the type comes from the
    catalogue row whose class the payload has, the bytes are ``asdict`` of it."""
    x = populated(cls)
    env = make("test-01", x)
    assert env.type is msg_type
    assert env.agent_id == "test-01"
    assert env.payload == asdict(x)
    assert Envelope.from_json(env.to_json()).payload == env.payload


def test_make_refuses_an_inbound_only_payload() -> None:
    """``AckPayload`` rides under three inbound types, so there is no outbound
    row to resolve: a caller handing one to ``make`` has the direction wrong."""
    with pytest.raises(TypeError, match="AckPayload is not an outbound payload"):
        make("test-01", AckPayload())


def test_a_payload_class_serving_two_outbound_types_is_refused_at_import() -> None:
    """The reverse of ``MESSAGES`` that ``make`` resolves with must be injective
    on this side, or a second outbound row for a class would let ``make`` pick
    one silently. The guard fires as the module loads, so a fresh interpreter
    grows the catalogue by one such row and reloads it."""
    script = textwrap.dedent(
        """
        import importlib
        import stormpulse.protocol.envelope as envelope
        import stormpulse.protocol.payloads as payloads
        from stormpulse.protocol.payloads import Direction, HeartbeatPayload
        from stormpulse.protocol.payloads import MessageSpec, MessageType
        payloads.MESSAGES = {
            **payloads.MESSAGES,
            MessageType.HEARTBEAT_ACK: MessageSpec(
                Direction.AGENT_TO_DASHBOARD, HeartbeatPayload
            ),
        }
        importlib.reload(envelope)
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[2],
        timeout=60,
    )
    assert proc.returncode == 1
    assert (
        "RuntimeError: MESSAGES: an outbound payload class serves two message types"
        in proc.stderr
    )


def test_make_refuses_a_metrics_payload(populated: Callable[[Any], Any]) -> None:
    """``metrics.push`` has one door: ``make`` would write ``"jobs": null`` where
    ``make_metrics_push`` leaves the key out, so the generic factory refuses it."""
    with pytest.raises(
        TypeError, match="MetricsPayload rides through make_metrics_push"
    ):
        make("test-01", populated(MetricsPayload))


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
    env = make("test-01", result)
    assert env.type == MessageType.COMMAND_RESULT
    Envelope.from_json(env.to_json())


def test_roundtrip_heartbeat() -> None:
    original = make("test-agent", HeartbeatPayload())
    rebuilt = Envelope.from_json(original.to_json())
    assert rebuilt.type == original.type
    assert rebuilt.agent_id == original.agent_id
    assert rebuilt.payload == original.payload


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
    original = make("test-agent", result)
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
    original = make("test-agent", result)
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
    original = make("test-agent", result)
    rebuilt = Envelope.from_json(original.to_json())
    assert rebuilt.payload["failure_reason"] == "timeout"
    payload = CommandResultPayload.from_dict(rebuilt.payload)
    assert payload.failure_reason is FailureReason.TIMEOUT


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
    original = make("test-agent", progress)
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
    original = make("test-agent", progress)
    rebuilt = Envelope.from_json(original.to_json())
    assert rebuilt.payload["total"] is None
    assert rebuilt.payload["message"] == ""


def test_log_batch_envelope() -> None:
    batch = LogBatchPayload(
        group="storage",
        parser="garage_s3",
        batch_id="b1",
        lines=[{"ts": "x", "bucket": "b"}],
        dropped=0,
        from_position=0,
        to_position=100,
    )
    env = make("agent-1", batch)
    assert env.type == MessageType.LOG_BATCH
    assert env.agent_id == "agent-1"
    assert env.payload["group"] == "storage"
    assert env.payload["batch_id"] == "b1"
    assert len(env.payload["lines"]) == 1


def test_log_batch_envelope_json_roundtrip() -> None:
    batch = LogBatchPayload(
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
    env = make("a", batch)
    raw = env.to_json()
    restored = Envelope.from_json(raw)
    assert restored.type == MessageType.LOG_BATCH
    payload = LogBatchPayload.from_dict(restored.payload)
    assert payload.batch_id == "bid"


# -- the three factories that keep logic -------------------------------------


def test_make_error_carries_reason_and_type_only() -> None:
    """The error reply is a reason code and the refused type: no payload, no raw frame."""
    env = make_error("test-01", "unknown_type", "command.undo")
    assert env.type is MessageType.ERROR
    assert env.v == SPEAKS[-1]
    assert env.payload == {"error": "unknown_type", "type": "command.undo"}
    assert make_error("test-01", "bad_envelope", None).payload["type"] is None


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


def test_register_with_log_groups() -> None:
    env = make_register(
        "agent-1", "0.3.0", "tok", **register_fields(log_groups=["storage", "pulse"])
    )
    assert env.payload["log_groups"] == ["storage", "pulse"]


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
    assert "jobs" not in env.payload  # absent, never null, with no job manager
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
        "test-01", replace(metrics, jobs={"pending": 8, "running": 6})
    )
    assert env.payload["jobs"] == {"pending": 8, "running": 6}
    Envelope.from_json(env.to_json())


def test_make_metrics_push_with_integrations_merges_dict() -> None:
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
    report = IntegrationReport(
        status="live",
        disabled_reason=None,
        state={"node_id": "abc", "healthy": True, "buckets": []},
        command_specs_digest="",
        commands_fenced=None,
        generation=1,
        complete=True,
        last_full_read_at="2026-10-09T00:00:00Z",
        changed_ids=[],
    )
    env = make_metrics_push(
        "agent-1", replace(metrics, integrations={"garage": report})
    )
    assert env.payload["integrations"] == {"garage": asdict(report)}
    assert "garage" not in env.payload
    # Full round-trip preserves integrations
    round_tripped = Envelope.from_json(env.to_json())
    assert round_tripped.payload["integrations"] == {"garage": asdict(report)}
    assert round_tripped.payload["cpu_percent"] == 1.0


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
    env = make_metrics_push(
        "test-01", replace(metrics, jobs={"pending": 8, "running": 6})
    )
    parsed = MetricsPayload.from_dict(Envelope.from_json(env.to_json()).payload)
    assert parsed.jobs == {"pending": 8, "running": 6}
    assert (
        MetricsPayload.from_dict(make_metrics_push("t", metrics).payload).jobs is None
    )


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
