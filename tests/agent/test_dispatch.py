"""Tests for the inbound message dispatcher."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from stormpulse import events
from stormpulse.agent import Agent, acks, dispatch
from stormpulse.agent.integrations_runtime import (
    merge_items_into_runtime,
    replace_runtime_state,
)
from stormpulse.auth import NonceStore
from stormpulse.commands import ParamValidationError
from stormpulse.config import CommandSpec, Config
from stormpulse.protocol import SPEAKS, MessageType, make_heartbeat
from stormpulse.signoff import SignoffState
from tests.helpers import (
    AGENT_ID,
    SECRET,
    envelope,
    make_failed_result,
    make_fake_garage_state,
    make_garage_bucket,
    make_successful_result,
    sign_command_request,
    sign_command_sequence,
    signed_command_sequence,
)

# ---------------------------------------------------------------------------
# Single command request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("stormpulse.agent.dispatch.execute_command")
async def test_dispatch_command_request(
    mock_exec: MagicMock,
    agent: Agent,
) -> None:
    mock_exec.return_value = make_successful_result()
    ws = AsyncMock()

    await dispatch.dispatch_message(agent, ws, sign_command_request())

    mock_exec.assert_called_once()
    ws.send.assert_called_once()
    sent_data = json.loads(ws.send.call_args[0][0])
    assert sent_data["type"] == "command.result"
    assert sent_data["payload"]["success"] is True


def _only_error_reply(ws: AsyncMock) -> dict[str, Any]:
    """The one frame sent, parsed; asserts it is an ``error`` and nothing else went out."""
    ws.send.assert_called_once()
    reply = json.loads(ws.send.call_args.args[0])
    assert reply["type"] == "error"
    assert reply["agent_id"] == AGENT_ID
    assert set(reply["payload"]) == {"error", "type"}
    return reply


@pytest.mark.asyncio
async def test_dispatch_bad_json(agent: Agent) -> None:
    """An unparseable frame is answered with ``bad_envelope`` and no type, the socket kept."""
    ws = AsyncMock()
    await dispatch.dispatch_message(agent, ws, "not json{{{")
    reply = _only_error_reply(ws)
    assert reply["payload"] == {"error": "bad_envelope", "type": None}
    assert "not json" not in ws.send.call_args.args[0]


@pytest.mark.asyncio
async def test_dispatch_bad_hmac(agent: Agent) -> None:
    ws = AsyncMock()
    env = envelope(
        MessageType.COMMAND_REQUEST,
        {"command": "git_pull", "params": {}, "hmac": "bad", "nonce": "n"},
    )
    await dispatch.dispatch_message(agent, ws, env.to_json())
    ws.send.assert_not_called()


@pytest.mark.asyncio
async def test_a_refused_signature_is_one_witnessed_event(agent: Agent) -> None:
    """The forgery gets no reply, but its record carries every declared field
    through the registry's filter (CORE-011 d6), the untrusted id capped at 100."""
    env = envelope(
        MessageType.COMMAND_REQUEST,
        {"command": "git_pull", "params": {}, "hmac": "bad", "nonce": "n"},
        id="i" * 200,
    )
    await dispatch.dispatch_message(agent, AsyncMock(), env.to_json())
    (e,) = events.buffer().drain("b")
    assert (e["source"], e["kind"]) == ("auth", "signature_failure")
    assert e["status"] == "refused"
    assert e["failure_reason"] == "bad_signature"
    assert e["error"] == "HMAC verification failed"
    assert e["command_ref"] == "i" * 100


@pytest.mark.asyncio
async def test_a_whitelist_rejection_is_one_alarming_event(agent: Agent) -> None:
    """An authenticated request outside the whitelist is the fleet's audit signal:
    one ``commands/whitelist_rejection`` naming the command, through the filter."""
    await dispatch.dispatch_message(
        agent, AsyncMock(), sign_command_request(command="this_command_does_not_exist")
    )
    rejected = [
        e for e in events.buffer().drain("b") if e["kind"] == "whitelist_rejection"
    ]
    assert len(rejected) == 1
    e = rejected[0]
    assert (e["source"], e["status"]) == ("commands", "refused")
    assert e["command"] == "this_command_does_not_exist"
    assert "this_command_does_not_exist" in e["error"]
    assert e["command_ref"]


@pytest.mark.asyncio
async def test_dispatch_unexpected_type(agent: Agent) -> None:
    """A known-but-outbound type sent to the agent gets one ``unknown_type`` error naming it."""
    ws = AsyncMock()
    heartbeat = make_heartbeat(AGENT_ID)
    await dispatch.dispatch_message(agent, ws, heartbeat.to_json())
    reply = _only_error_reply(ws)
    assert reply["payload"] == {"error": "unknown_type", "type": "heartbeat"}


@pytest.mark.asyncio
async def test_dispatch_unknown_type_string_is_answered_once(agent: Agent) -> None:
    """A type outside the catalogue gets exactly one ``unknown_type`` reply quoting the type."""
    ws = AsyncMock()
    frame = envelope(MessageType.HEARTBEAT, {}).to_dict()
    frame["type"] = "command.undo"
    await dispatch.dispatch_message(agent, ws, json.dumps(frame))
    reply = _only_error_reply(ws)
    assert reply["payload"] == {"error": "unknown_type", "type": "command.undo"}


@pytest.mark.asyncio
async def test_dispatch_unsupported_version_is_answered(agent: Agent) -> None:
    ws = AsyncMock()
    frame = envelope(MessageType.HEARTBEAT_ACK, {}).to_dict()
    frame["v"] = SPEAKS[-1] + 1
    await dispatch.dispatch_message(agent, ws, json.dumps(frame))
    reply = _only_error_reply(ws)
    assert reply["payload"] == {"error": "unsupported_version", "type": None}


@pytest.mark.asyncio
async def test_dispatch_error_reply_never_echoes_the_payload(agent: Agent) -> None:
    """A 1 MB payload on an unknown type gets a reply under 300 bytes: reason and type only."""
    ws = AsyncMock()
    frame = envelope(MessageType.HEARTBEAT, {"blob": "x" * 1_000_000}).to_dict()
    frame["type"] = "y" * 1_000
    await dispatch.dispatch_message(agent, ws, json.dumps(frame))
    sent = ws.send.call_args.args[0]
    assert len(sent) < 300
    assert "x" * 100 not in sent
    assert _only_error_reply(ws)["payload"]["type"] == "y" * 64


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ack_type",
    [
        MessageType.REGISTER_OK,
        MessageType.HEARTBEAT_ACK,
        MessageType.COMMAND_RESULT_ACK,
        MessageType.SIGNOFF_STATE_ACK,
        MessageType.ERROR,
    ],
)
async def test_dispatch_ack_types_ignored(
    agent: Agent,
    ack_type: MessageType,
) -> None:
    ws = AsyncMock()
    await dispatch.dispatch_message(agent, ws, envelope(ack_type, {}).to_json())
    ws.send.assert_not_called()


@pytest.mark.asyncio
async def test_inbound_error_is_a_warning_naming_reason_and_type_never_a_reply(
    agent: Agent, caplog: pytest.LogCaptureFixture
) -> None:
    """The control plane refused a frame of ours (CORE-011 d2): said at warning,
    never at debug like an ack, and never answered, or two refusals would ping-pong."""
    ws = AsyncMock()
    frame = envelope(
        MessageType.ERROR, {"error": "unknown_type", "type": "metrics.push"}, id="e-1"
    )
    with caplog.at_level(logging.DEBUG, logger="stormpulse.agent.acks"):
        await dispatch.dispatch_message(agent, ws, frame.to_json())
    ws.send.assert_not_called()
    said = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(said) == 1
    assert "unknown_type" in said[0].getMessage()
    assert "metrics.push" in said[0].getMessage()
    assert "e-1" in said[0].getMessage()


# ---------------------------------------------------------------------------
# Dispatch-time seal refusal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("stormpulse.agent.dispatch.execute_command")
async def test_dispatch_refuses_run_verify_block_when_sealed(
    mock_exec: MagicMock,
    agent: Agent,
) -> None:
    agent.signoff_state.seal()
    ws = AsyncMock()

    await dispatch.dispatch_message(
        agent,
        ws,
        sign_command_request(
            command="run_verify_block",
            params={"verify_command": "echo ok"},
        ),
    )

    mock_exec.assert_not_called()
    ws.send.assert_called_once()
    sent_data = json.loads(ws.send.call_args[0][0])
    assert sent_data["type"] == "command.result"
    assert sent_data["payload"]["success"] is False
    assert sent_data["payload"]["failure_reason"] == "signoff_sealed"
    assert sent_data["payload"]["exit_code"] == -1


@pytest.mark.asyncio
@patch("stormpulse.agent.dispatch.execute_command")
async def test_dispatch_refuses_run_apply_block_when_sealed(
    mock_exec: MagicMock,
    agent: Agent,
) -> None:
    agent.signoff_state.seal()
    ws = AsyncMock()

    await dispatch.dispatch_message(
        agent,
        ws,
        sign_command_request(
            command="run_apply_block",
            params={"apply_command": "echo ok"},
        ),
    )

    mock_exec.assert_not_called()
    ws.send.assert_called_once()
    sent_data = json.loads(ws.send.call_args[0][0])
    assert sent_data["type"] == "command.result"
    assert sent_data["payload"]["command"] == "run_apply_block"
    assert sent_data["payload"]["success"] is False
    assert sent_data["payload"]["failure_reason"] == "signoff_sealed"
    assert sent_data["payload"]["exit_code"] == -1


@pytest.mark.asyncio
@patch("stormpulse.agent.dispatch.execute_command")
async def test_dispatch_runs_run_apply_block_when_unsealed(
    mock_exec: MagicMock,
    agent: Agent,
) -> None:
    mock_exec.return_value = make_successful_result(command="run_apply_block")
    ws = AsyncMock()

    await dispatch.dispatch_message(
        agent,
        ws,
        sign_command_request(
            command="run_apply_block",
            params={"apply_command": "echo ok"},
        ),
    )

    mock_exec.assert_called_once()
    ws.send.assert_called_once()
    sent_data = json.loads(ws.send.call_args[0][0])
    assert sent_data["type"] == "command.result"
    assert sent_data["payload"]["success"] is True


# ---------------------------------------------------------------------------
# Command sequence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("stormpulse.agent.dispatch.execute_command")
async def test_dispatch_command_sequence(
    mock_exec: MagicMock,
    agent: Agent,
) -> None:
    mock_exec.return_value = make_successful_result()
    ws = AsyncMock()

    await dispatch.dispatch_message(
        agent, ws, sign_command_sequence(["git_pull", "docker_logs"])
    )

    assert mock_exec.call_count == 2
    assert ws.send.call_count == 2


@pytest.mark.asyncio
@patch("stormpulse.agent.dispatch.execute_command")
async def test_dispatch_sequence_stop_on_failure(
    mock_exec: MagicMock,
    agent: Agent,
) -> None:
    mock_exec.return_value = make_failed_result(command="git_pull")
    ws = AsyncMock()

    await dispatch.dispatch_message(
        agent,
        ws,
        sign_command_sequence(["git_pull", "docker_logs"], stop_on_failure=True),
    )

    assert mock_exec.call_count == 1
    assert ws.send.call_count == 1


@pytest.mark.asyncio
@patch("stormpulse.agent.dispatch.execute_command")
async def test_dispatch_sequence_continues_past_failure(
    mock_exec: MagicMock,
    agent: Agent,
) -> None:
    mock_exec.return_value = make_failed_result(command="git_pull")
    ws = AsyncMock()

    await dispatch.dispatch_message(
        agent,
        ws,
        sign_command_sequence(["git_pull", "docker_logs"], stop_on_failure=False),
    )

    assert mock_exec.call_count == 2
    assert ws.send.call_count == 2


@pytest.mark.asyncio
@patch("stormpulse.agent.dispatch.execute_command")
async def test_dispatch_sequence_invalid_command_sends_failure(
    mock_exec: MagicMock,
    agent: Agent,
) -> None:
    """An authenticated sequence with a bad command gets a wire failure, not silence."""
    ws = AsyncMock()
    await dispatch.dispatch_message(
        agent, ws, sign_command_sequence(["this_command_does_not_exist"])
    )
    mock_exec.assert_not_called()
    ws.send.assert_called_once()
    sent_data = json.loads(ws.send.call_args[0][0])
    assert sent_data["type"] == "command.result"
    assert sent_data["payload"]["success"] is False
    assert sent_data["payload"]["failure_reason"] == "unknown_command"
    assert sent_data["payload"]["command"] == "this_command_does_not_exist"
    assert sent_data["payload"]["sequence_id"]


@pytest.mark.asyncio
@patch("stormpulse.agent.dispatch.execute_command")
async def test_dispatch_sequence_refuses_job_command(
    mock_exec: MagicMock,
    agent: Agent,
) -> None:
    """A job in a sequence is refused before any step runs, never PATH-resolved."""
    agent.registry["some_job"] = CommandSpec(
        group="g", command=["some_job"], timeout=5, mode="job", handler=lambda p: None
    )
    ws = AsyncMock()
    await dispatch.dispatch_message(
        agent, ws, sign_command_sequence(["git_pull", "some_job"])
    )
    mock_exec.assert_not_called()
    sent_data = json.loads(ws.send.call_args[0][0])
    assert sent_data["payload"]["failure_reason"] == "forbidden"
    assert sent_data["payload"]["command"] == "some_job"


@pytest.mark.asyncio
async def test_dispatch_unknown_command_sends_failure(agent: Agent) -> None:
    """An authenticated request for an unknown command gets a wire failure,
    named ``unknown_command`` (CORE-011 d9), distinct from a bad param."""
    ws = AsyncMock()
    await dispatch.dispatch_message(
        agent, ws, sign_command_request(command="this_command_does_not_exist")
    )
    ws.send.assert_called_once()
    sent_data = json.loads(ws.send.call_args[0][0])
    assert sent_data["type"] == "command.result"
    assert sent_data["payload"]["success"] is False
    assert sent_data["payload"]["failure_reason"] == "unknown_command"
    assert sent_data["payload"]["error"]["code"] == "unknown_command"


@pytest.mark.asyncio
async def test_dispatch_bad_param_is_validation_failed_not_unknown(
    agent: Agent,
) -> None:
    """A known command with a bad param is an ordinary refusal: ``validation_failed``,
    never the whitelist's ``unknown_command``."""
    ws = AsyncMock()
    await dispatch.dispatch_message(
        agent,
        ws,
        sign_command_request(
            command="docker_logs", params={"docker_service_name": "a b"}
        ),
    )
    sent_data = json.loads(ws.send.call_args[0][0])
    assert sent_data["payload"]["success"] is False
    assert sent_data["payload"]["failure_reason"] == "validation_failed"


@pytest.mark.asyncio
@patch("stormpulse.agent.dispatch.execute_command")
async def test_dispatch_sealed_sequence_sends_refusal(
    mock_exec: MagicMock,
    agent: Agent,
) -> None:
    """A sealed sequence sends the refusal on the wire, mirroring the single path."""
    agent.signoff_state.seal()
    ws = AsyncMock()
    await dispatch.dispatch_message(
        agent, ws, sign_command_sequence(["git_pull", "run_apply_block"])
    )
    mock_exec.assert_not_called()
    ws.send.assert_called_once()
    sent_data = json.loads(ws.send.call_args[0][0])
    assert sent_data["type"] == "command.result"
    assert sent_data["payload"]["success"] is False
    assert sent_data["payload"]["failure_reason"] == "signoff_sealed"
    assert sent_data["payload"]["command"] == "run_apply_block"
    assert sent_data["payload"]["sequence_id"]


# ---------------------------------------------------------------------------
# Unseal-before-restart: hatch command missing from the boot-built registry
# while the agent is unsealed (ADR CORE-004). Simulated by dropping the
# command from the registry, which is what a boot-time-sealed agent has.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("stormpulse.agent.dispatch.execute_command")
async def test_dispatch_deflects_unsealed_hatch_awaiting_restart(
    mock_exec: MagicMock,
    agent: Agent,
) -> None:
    """Unsealed but the command is not loaded: ask for a restart, do not alarm."""
    assert not agent.signoff_state.is_sealed()
    agent.registry.pop("run_verify_block", None)
    ws = AsyncMock()

    await dispatch.dispatch_message(
        agent,
        ws,
        sign_command_request(
            command="run_verify_block",
            params={"verify_command": "echo ok"},
        ),
    )

    mock_exec.assert_not_called()
    ws.send.assert_called_once()
    sent_data = json.loads(ws.send.call_args[0][0])
    assert sent_data["type"] == "command.result"
    assert sent_data["payload"]["success"] is False
    assert sent_data["payload"]["failure_reason"] == "restart_required"
    assert sent_data["payload"]["command"] == "run_verify_block"


@pytest.mark.asyncio
@patch("stormpulse.agent.dispatch.execute_command")
async def test_dispatch_sequence_deflects_unsealed_hatch_awaiting_restart(
    mock_exec: MagicMock,
    agent: Agent,
) -> None:
    """Sequence path mirrors the single path: restart_required before whitelist alarm."""
    assert not agent.signoff_state.is_sealed()
    agent.registry.pop("run_apply_block", None)
    ws = AsyncMock()

    await dispatch.dispatch_message(
        agent, ws, sign_command_sequence(["git_pull", "run_apply_block"])
    )

    mock_exec.assert_not_called()
    ws.send.assert_called_once()
    sent_data = json.loads(ws.send.call_args[0][0])
    assert sent_data["type"] == "command.result"
    assert sent_data["payload"]["success"] is False
    assert sent_data["payload"]["failure_reason"] == "restart_required"
    assert sent_data["payload"]["command"] == "run_apply_block"
    assert sent_data["payload"]["sequence_id"]


# ---------------------------------------------------------------------------
# log.batch.ack
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_batch_ack_missing_batch_id(agent: Agent) -> None:
    env = envelope(MessageType.LOG_BATCH_ACK, {}, id="x")
    # Should not raise
    await acks.handle_log_batch_ack(agent, AsyncMock(), env)


@pytest.mark.asyncio
async def test_log_batch_ack_unknown_batch_is_noop(agent: Agent) -> None:
    env = envelope(MessageType.LOG_BATCH_ACK, {"batch_id": "never-sent"}, id="x")
    await acks.handle_log_batch_ack(agent, AsyncMock(), env)


@pytest.mark.asyncio
async def test_log_batch_ack_advances_position(agent: Agent) -> None:
    fake_shipper = MagicMock()
    fake_shipper.tailer.confirm_shipped = MagicMock()
    agent.shippers["grp"] = fake_shipper
    agent.pending_batches.add("bid-1", "grp", 4242)

    env = envelope(MessageType.LOG_BATCH_ACK, {"batch_id": "bid-1"}, id="x")
    await acks.handle_log_batch_ack(agent, AsyncMock(), env)

    fake_shipper.tailer.confirm_shipped.assert_called_once_with(4242)
    assert "bid-1" not in agent.pending_batches


# ---------------------------------------------------------------------------
# PulseLogger integration on command result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("stormpulse.agent.dispatch.execute_command")
async def test_command_result_logged_to_pulse_logger(
    mock_exec: MagicMock,
    config: Config,
    nonce_store: NonceStore,
    ssl_ctx: MagicMock,
    shutdown: asyncio.Event,
) -> None:
    mock_exec.return_value = make_successful_result()
    pulse_logger = MagicMock()
    ag = Agent(
        config,
        SECRET,
        nonce_store,
        ssl_ctx,
        shutdown,
        signoff_state=SignoffState(config.storage.db_path.parent),
        pulse_logger=pulse_logger,
    )
    ws = AsyncMock()
    await dispatch.dispatch_message(ag, ws, sign_command_request())
    pulse_logger.log_command_result.assert_called_once()
    kwargs = pulse_logger.log_command_result.call_args.kwargs
    assert kwargs["command"] == "git_pull"
    assert kwargs["success"] is True


def test_every_inbound_type_is_routed_and_nothing_outbound_is() -> None:
    """The dispatcher is exhaustive over the catalogue: a new inbound type fails here until routed."""
    from stormpulse.protocol import MESSAGES, Direction

    inbound = {
        t
        for t, row in MESSAGES.items()
        if row.direction in (Direction.DASHBOARD_TO_AGENT, Direction.BOTH)
    }
    assert set(dispatch.HANDLERS) == inbound
    assert MESSAGES[MessageType.ERROR].direction is Direction.BOTH


# ---------------------------------------------------------------------------
# metrics.ack
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_metrics_ack_releases_only_the_identified_push(
    agent_with_garage: Callable[..., Agent],
) -> None:
    ag = agent_with_garage()
    rt = ag.integrations["garage"]
    from stormpulse.agent.integrations_runtime import track_push
    from stormpulse.integrations import Freshness

    replace_runtime_state(
        rt, replace(make_fake_garage_state(), read=Freshness(changed_ids=("a" * 64,)))
    )
    track_push(ag.integrations, "push-1")
    merge_items_into_runtime(rt, [make_garage_bucket("b" * 64)])
    ws = AsyncMock()
    await dispatch.dispatch_message(
        ag, ws, envelope(MessageType.METRICS_ACK, {"push_id": "push-1"}).to_json()
    )
    assert rt.changed_ids == ["b" * 64]
    ws.send.assert_not_called()


# ---------------------------------------------------------------------------
# events.batch.nack
# ---------------------------------------------------------------------------


def _nack(batch_id: str, reason: str = "schema_rejected") -> str:
    return envelope(
        MessageType.EVENTS_BATCH_NACK, {"batch_id": batch_id, "reason": reason}
    ).to_json()


@pytest.mark.asyncio
async def test_events_batch_nack_frees_the_slot_and_says_so_once(agent: Agent) -> None:
    """CORE-011 d6: a refused batch is dropped, never re-sent, and the loss is one
    ``events_nacked`` carrying the control plane's reason, not the batch again."""
    buf = events.buffer()
    buf.append({"kind": "a"})
    buf.append({"kind": "b"})
    buf.drain("b1")
    ws = AsyncMock()
    await dispatch.dispatch_message(agent, ws, _nack("b1", "forbidden_identifier"))
    ws.send.assert_not_called()
    assert buf.ack("b1") is False
    assert buf.requeue_unacked() == 0
    batch = buf.drain("b2")
    assert [e["kind"] for e in batch] == ["events_nacked"]
    nacked = batch[0]
    assert nacked["source"] == "events"
    assert nacked["batch_id"] == "b1"
    assert nacked["dropped"] == 2
    assert nacked["reason"] == "forbidden_identifier"


@pytest.mark.asyncio
async def test_events_batch_nack_caps_the_reason_like_error(agent: Agent) -> None:
    buf = events.buffer()
    buf.append({"kind": "a"})
    buf.drain("b1")
    await dispatch.dispatch_message(agent, AsyncMock(), _nack("b1", "x" * 10_000))
    assert len(buf.drain("b2")[0]["reason"]) == 500


@pytest.mark.asyncio
async def test_events_batch_nack_without_a_reason_rides_sparse(agent: Agent) -> None:
    """A nack that gives no reason drops the batch all the same; the event
    carries no ``reason`` key rather than the string ``"None"``."""
    buf = events.buffer()
    buf.append({"kind": "a"})
    buf.drain("b1")
    env = envelope(MessageType.EVENTS_BATCH_NACK, {"batch_id": "b1"})
    await dispatch.dispatch_message(agent, AsyncMock(), env.to_json())
    (nacked,) = buf.drain("b2")
    assert (nacked["kind"], nacked["dropped"]) == ("events_nacked", 1)
    assert "reason" not in nacked


@pytest.mark.asyncio
async def test_events_batch_nack_for_an_unknown_batch_is_a_debug_log(
    agent: Agent, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger="stormpulse.agent.acks"):
        await dispatch.dispatch_message(agent, AsyncMock(), _nack("never-sent"))
    said = [r for r in caplog.records if "never-sent" in r.getMessage()]
    assert [r.levelno for r in said] == [logging.DEBUG]
    assert events.buffer().drain("b2") == []


@pytest.mark.asyncio
async def test_events_batch_nack_missing_batch_id_is_a_warning_not_a_raise(
    agent: Agent, caplog: pytest.LogCaptureFixture
) -> None:
    env = envelope(MessageType.EVENTS_BATCH_NACK, {"reason": "r"}, id="x")
    with caplog.at_level(logging.WARNING, logger="stormpulse.agent.acks"):
        await acks.handle_events_batch_nack(agent, AsyncMock(), env)
    assert any("missing batch_id" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Sequence steps: params per step, step_index on every result (CORE-011 d9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("stormpulse.agent.dispatch.execute_command")
async def test_sequence_results_carry_step_index_and_one_sequence_id(
    mock_exec: MagicMock,
    agent: Agent,
) -> None:
    """Two steps: ``step_index`` 0 and 1, the same ``sequence_id`` on both, and
    each step's params reach ``execute_command`` as its runtime params."""

    def run(name: str, cfg: Any, rid: str, seq: str, **kw: Any) -> Any:
        return make_successful_result(command=name, request_id=rid, sequence_id=seq)

    mock_exec.side_effect = run
    ws = AsyncMock()
    frame = signed_command_sequence(
        steps=[
            {"command": "git_pull", "params": {}},
            {"command": "docker_logs", "params": {"tail": "5"}},
        ],
        sequence_id="seq-7",
    )
    await dispatch.dispatch_message(agent, ws, frame.to_json())

    sent = [json.loads(c.args[0])["payload"] for c in ws.send.call_args_list]
    assert [(p["command"], p["step_index"], p["sequence_id"]) for p in sent] == [
        ("git_pull", 0, "seq-7"),
        ("docker_logs", 1, "seq-7"),
    ]
    assert len({p["request_id"] for p in sent}) == 2
    assert [c.kwargs["runtime_params"] for c in mock_exec.call_args_list] == [
        None,
        {"tail": "5"},
    ]


@pytest.mark.asyncio
@patch("stormpulse.agent.dispatch.execute_command")
async def test_a_step_with_a_bad_param_fails_on_the_wire_and_halts_the_sequence(
    mock_exec: MagicMock,
    agent: Agent,
) -> None:
    """A ``ParamValidationError`` in a step is a ``validation_failed`` result for
    that step, never an exception the receive loop swallows into silence."""
    mock_exec.side_effect = [
        ParamValidationError("depth: does not match pattern"),
        make_successful_result(),
    ]
    ws = AsyncMock()
    await dispatch.dispatch_message(
        agent,
        ws,
        sign_command_sequence(["git_pull", "docker_logs"], stop_on_failure=True),
    )
    assert mock_exec.call_count == 1
    sent = [json.loads(c.args[0])["payload"] for c in ws.send.call_args_list]
    assert len(sent) == 1
    assert (sent[0]["command"], sent[0]["step_index"], sent[0]["failure_reason"]) == (
        "git_pull",
        0,
        "validation_failed",
    )
    assert sent[0]["group"] == agent.registry["git_pull"].group
    assert "depth" in sent[0]["stderr"]


@pytest.mark.asyncio
@patch("stormpulse.agent.dispatch.execute_command")
async def test_sequence_refusals_name_the_step_they_answer_for(
    mock_exec: MagicMock,
    agent: Agent,
) -> None:
    """Restart, unknown and sealed refusals carry the offending step's index."""

    async def refusal(names: list[str]) -> dict[str, Any]:
        ws = AsyncMock()
        await dispatch.dispatch_message(agent, ws, sign_command_sequence(names))
        ws.send.assert_called_once()
        return json.loads(ws.send.call_args.args[0])["payload"]

    agent.registry.pop("run_apply_block", None)
    restart = await refusal(["git_pull", "run_apply_block"])
    assert (restart["failure_reason"], restart["step_index"]) == ("restart_required", 1)

    unknown = await refusal(["git_pull", "docker_logs", "nope"])
    assert (unknown["failure_reason"], unknown["step_index"]) == ("unknown_command", 2)

    agent.signoff_state.seal()
    sealed = await refusal(["git_pull", "run_verify_block"])
    assert (sealed["failure_reason"], sealed["step_index"]) == ("signoff_sealed", 1)
    mock_exec.assert_not_called()
