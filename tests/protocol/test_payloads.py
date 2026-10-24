# skylos: ignore[SKY-Q502] one test module per source module; the payload catalogue has this many seams
"""Tests for ``stormpulse.protocol.payloads``: the catalogue, each payload from
its dict, and the rules ``command.progress`` and ``command.result`` carry
(CORE-011 d3, d9).
"""

from __future__ import annotations

from dataclasses import asdict, fields
from typing import Any

import pytest

from stormpulse.protocol import (
    MESSAGES,
    CancelReason,
    CommandCancelledPayload,
    CommandProgressPayload,
    CommandRequestPayload,
    CommandResultPayload,
    CommandSequencePayload,
    ContainerInfo,
    Envelope,
    FailureReason,
    MessageType,
    MetricsPayload,
    ProtocolError,
    RegisterPayload,
    ResultError,
    Step,
    TransferStats,
    make,
)
from tests.helpers import register_payload

# -- the catalogue (CORE-011 d3) ---------------------------------------------


def test_every_message_type_is_catalogued() -> None:
    from dataclasses import is_dataclass

    from stormpulse.protocol import MESSAGES, WirePayload

    assert set(MESSAGES) == set(MessageType)
    assert all(
        is_dataclass(row.payload) and issubclass(row.payload, WirePayload)
        for row in MESSAGES.values()
    )


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


def test_log_batch_ack_message_type_valid() -> None:
    assert MessageType("log.batch.ack") is MessageType.LOG_BATCH_ACK


def test_builders_emit_the_payload_class_the_catalogue_names() -> None:
    from stormpulse.protocol import MESSAGES, EventsBatchPayload

    assert MESSAGES[MessageType.EVENTS_BATCH].payload is EventsBatchPayload
    batch = make("test-01", EventsBatchPayload(batch_id="b1", events=[{"kind": "x"}]))
    assert batch.payload == {"batch_id": "b1", "events": [{"kind": "x"}]}
    assert {f.name for f in fields(EventsBatchPayload)} == set(batch.payload)


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


def test_command_cancelled_is_outbound_and_names_the_job_and_the_reason() -> None:
    """One frame per job that will send no result: its id, its command and why."""
    from stormpulse.protocol import Direction

    spec = MESSAGES[MessageType.COMMAND_CANCELLED]
    assert spec.direction is Direction.AGENT_TO_DASHBOARD
    assert spec.payload is CommandCancelledPayload
    env = make(
        "a1",
        CommandCancelledPayload(
            request_id="r1", command="garage_bucket_clear", reason=CancelReason.SHUTDOWN
        ),
    )
    assert env.type is MessageType.COMMAND_CANCELLED
    assert Envelope.from_json(env.to_json()).payload == {
        "request_id": "r1",
        "command": "garage_bucket_clear",
        "reason": "shutdown",
    }
    assert {r.value for r in CancelReason} == {"shutdown", "operator"}


# -- each payload from its dict ----------------------------------------------


def test_metrics_payload_from_dict(metrics_dict: dict[str, Any]) -> None:
    payload = MetricsPayload.from_dict(metrics_dict["payload"])
    assert payload.cpu_percent == 23.5
    assert len(payload.containers) == 1
    assert payload.containers[0].name == "web"


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


def test_container_info_from_dict() -> None:
    info = ContainerInfo.from_dict(
        {"name": "db", "status": "exited", "image": "postgres:16"}
    )
    assert info.name == "db"


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


def test_payload_immutable() -> None:
    payload = RegisterPayload.from_dict(register_payload())
    with pytest.raises(AttributeError):
        payload.version = "0.2.0"  # type: ignore[misc]


# -- command.progress --------------------------------------------------------


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


# -- command.result (CORE-011 d9) --------------------------------------------


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


def test_step_index_rides_a_result_and_is_null_outside_a_sequence(
    command_result_dict: dict[str, Any],
) -> None:
    single = CommandResultPayload.from_dict(command_result_dict["payload"])
    assert single.step_index is None
    assert make("a1", single).payload["step_index"] is None
    step = CommandResultPayload.from_dict(
        {**command_result_dict["payload"], "sequence_id": "s", "step_index": 1}
    )
    sent = make("a1", step).payload
    assert (sent["sequence_id"], sent["step_index"]) == ("s", 1)


def test_a_result_that_ran_once_still_sends_attempts_and_retries_exhausted(
    command_result_dict: dict[str, Any],
) -> None:
    """CORE-012: every result says how many runs stand behind it, so a reader
    never guesses at an absent key. A result that never asked to run again
    sends one attempt and no exhausted retries."""
    single = CommandResultPayload.from_dict(command_result_dict["payload"])
    sent = Envelope.from_json(make("a1", single).to_json()).payload
    assert sent["attempts"] == 1
    assert sent["retries_exhausted"] is False


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


def test_command_result_carries_the_figures_under_result_only() -> None:
    """CORE-011 d9: a job's extras ride under ``result``, never the top level,
    so the clobber mutant (an extra named ``success``) changes nothing standard."""
    payload = Envelope.from_json(
        make("t", _result(result={"deleted_count": 3, "success": False})).to_json()
    ).payload
    assert payload["result"] == {"deleted_count": 3, "success": False}
    assert payload["success"] is True
    assert "deleted_count" not in payload
    assert payload["error"] is None
    assert make("t", _result()).payload["result"] is None


def test_command_result_failure_derives_its_error_from_reason_and_stderr() -> None:
    failed = _result(
        success=False,
        exit_code=-1,
        stderr="UpdateBucket quota failed: 503",
        failure_reason=FailureReason.OS_ERROR,
    )
    payload = Envelope.from_json(make("t", failed).to_json()).payload
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
    bare = make("t", _result(success=False, exit_code=1)).payload
    assert bare["error"]["code"] == "unspecified"
    parsed = ResultError("bucket_delete_failed", 404, "NoSuchBucket", "gone")
    finer = _result(success=False, failure_reason=FailureReason.OS_ERROR, error=parsed)
    assert make("t", finer).payload["error"] == asdict(parsed)


def test_command_result_refuses_an_error_on_a_success() -> None:
    with pytest.raises(ValueError, match="cannot succeed with an error"):
        _result(error=ResultError("x", None, None, ""))


def test_failed_is_the_no_output_failure_shape_with_its_error_derived() -> None:
    """One constructor for every refusal: fixed figures, the message as stderr,
    the error derived by ``__post_init__``; the keyword extras ride through."""
    bare = CommandResultPayload.failed("r", "c", "g", FailureReason.NOT_FOUND, "gone")
    assert (bare.success, bare.exit_code, bare.stdout, bare.duration_ms) == (
        False,
        -1,
        "",
        0,
    )
    assert bare.stderr == "gone"
    assert bare.failure_reason is FailureReason.NOT_FOUND
    assert bare.error == ResultError("not_found", None, None, "gone")
    assert (bare.sequence_id, bare.step_index) == (None, None)
    placed = CommandResultPayload.failed(
        "r",
        "c",
        "g",
        FailureReason.TIMEOUT,
        "slow",
        sequence_id="s",
        step_index=2,
        duration_ms=7,
    )
    assert (placed.sequence_id, placed.step_index, placed.duration_ms) == ("s", 2, 7)
    assert make("t", placed).payload == asdict(placed)


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
