"""Tests for the inbound message dispatcher."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from stormpulse import events
from stormpulse.agent import Agent, acks, dispatch
from stormpulse.agent.integrations_runtime import (
    discard_pending_pushes,
    merge_items_into_runtime,
    replace_runtime_state,
    track_push,
)
from stormpulse.auth import NonceStore
from stormpulse.commands import ParamValidationError
from stormpulse.commands.jobs import JobManager, JobOutcome
from stormpulse.config import CommandSpec, Config, LogGroupConfig
from stormpulse.integrations import Freshness
from stormpulse.logging.positions import LogPositionStore
from stormpulse.logging.receipt import Receipt
from stormpulse.logging.shipper import LogShipper
from stormpulse.logging.tailer import LogTailer
from stormpulse.protocol import (
    SPEAKS,
    CancelReason,
    HeartbeatPayload,
    MessageType,
    ProtocolError,
    make,
)
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
    signed,
    signed_command_sequence,
    unsealed_state,
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
    heartbeat = make(AGENT_ID, HeartbeatPayload())
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


def _sealed_records() -> list[dict[str, Any]]:
    return [e for e in events.buffer().drain("b") if e["kind"] == "sealed_refusal"]


@pytest.mark.asyncio
async def test_a_sealed_refusal_is_one_record_not_an_alarm(agent: Agent) -> None:
    """A sealed single command leaves one ``signoff/sealed_refusal`` keyed to the
    envelope, carrying no params (shell text), and fires no whitelist alarm."""
    agent.signoff_state.seal()
    events.buffer().drain("b")
    frame = sign_command_request(
        command="run_verify_block", params={"verify_command": "echo s3cret"}
    )
    with patch("stormpulse.agent.dispatch.execute_command") as ex:
        await dispatch.dispatch_message(agent, AsyncMock(), frame)
    ex.assert_not_called()
    drained = events.buffer().drain("b")
    (e,) = [x for x in drained if x["kind"] == "sealed_refusal"]
    assert not [x for x in drained if x["kind"] == "whitelist_rejection"]
    assert (e["source"], e["status"], e["command"]) == (
        "signoff",
        "refused",
        "run_verify_block",
    )
    assert "unseal" in e["error"]
    assert e["command_ref"] == json.loads(frame)["id"]
    assert "s3cret" not in json.dumps(e)


@pytest.mark.asyncio
async def test_a_sealed_sequence_leaves_one_record_keyed_to_its_envelope(
    agent: Agent,
) -> None:
    """The whole-batch seal refusal records too, keyed to the sequence envelope."""
    agent.signoff_state.seal()
    events.buffer().drain("b")
    frame = signed_command_sequence(["git_pull", "run_apply_block"])
    with patch("stormpulse.agent.dispatch.execute_command") as ex:
        await dispatch.dispatch_message(agent, AsyncMock(), frame.to_json())
    ex.assert_not_called()
    (e,) = _sealed_records()
    assert (e["command"], e["command_ref"]) == ("run_apply_block", frame.id)


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


def _wire_results(ws: AsyncMock) -> list[dict[str, Any]]:
    return [json.loads(c.args[0])["payload"] for c in ws.send.call_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_on_failure", [True, False])
@pytest.mark.parametrize("hatch", ["run_verify_block", "run_apply_block"])
@pytest.mark.parametrize("reseal_in", ["execution", "result transmission"])
async def test_a_reseal_mid_sequence_refuses_the_next_hatch_step(
    agent: Agent, *, hatch: str, stop_on_failure: bool, reseal_in: str
) -> None:
    """Admitted unsealed, resealed after step 0: the hatch at step 1 is refused
    and the sequence ends, whatever stop_on_failure says (CORE-004 d6)."""
    ws = AsyncMock()

    def run(*_a: Any, **_k: Any) -> Any:
        if reseal_in == "execution":
            agent.signoff_state.seal()
        return make_successful_result()

    async def send(_frame: str) -> None:
        if reseal_in == "result transmission":
            agent.signoff_state.seal()

    ws.send.side_effect = send
    frame = signed_command_sequence(
        ["git_pull", hatch, "git_pull"], stop_on_failure=stop_on_failure
    )
    with patch("stormpulse.agent.dispatch.execute_command", side_effect=run) as ex:
        await dispatch.dispatch_message(agent, ws, frame.to_json())

    ex.assert_called_once()
    first, second = _wire_results(ws)
    assert first["success"] is True
    assert second["failure_reason"] == "signoff_sealed"
    assert second["command"] == hatch
    assert second["sequence_id"] == frame.payload["sequence_id"]
    assert second["step_index"] == 1
    (e,) = _sealed_records()
    assert (e["command"], e["command_ref"]) == (hatch, frame.id)


@pytest.mark.asyncio
async def test_a_revoke_mid_sequence_ends_it_and_alarms(agent: Agent) -> None:
    """A fence revoked between steps ends the sequence even with
    stop_on_failure=false, and the fence's alarm fires on this path too."""
    revoked: list[str] = []
    agent.registry["git_pull"] = replace(
        agent.registry["git_pull"], authorize=lambda: revoked[0] if revoked else None
    )

    def run(*_a: Any, **_k: Any) -> Any:
        revoked.append("command_contributor revoked")
        return make_successful_result()

    ws = AsyncMock()
    events.buffer().drain("b")
    frame = signed_command_sequence(
        ["docker_logs", "git_pull", "docker_logs"], stop_on_failure=False
    )
    with patch("stormpulse.agent.dispatch.execute_command", side_effect=run) as ex:
        await dispatch.dispatch_message(agent, ws, frame.to_json())

    ex.assert_called_once()
    first, second = _wire_results(ws)
    assert (second["step_index"], second["failure_reason"]) == (1, "forbidden")
    (alarm,) = [
        e for e in events.buffer().drain("b") if e["kind"] == "whitelist_rejection"
    ]
    assert alarm["command"] == "git_pull"


@pytest.mark.asyncio
async def test_a_mid_sequence_fence_alarm_names_the_sequence_envelope(
    agent: Agent,
) -> None:
    """The alarm's ``command_ref`` is the envelope the dashboard sent, never the
    step's fresh uuid, so the dashboard can match it to the run (Q5 A)."""
    revoked: list[str] = []
    agent.registry["git_pull"] = replace(
        agent.registry["git_pull"], authorize=lambda: revoked[0] if revoked else None
    )

    def run(*_a: Any, **_k: Any) -> Any:
        revoked.append("command_contributor revoked")
        return make_successful_result()

    events.buffer().drain("b")
    frame = signed_command_sequence(["docker_logs", "git_pull"])
    with patch("stormpulse.agent.dispatch.execute_command", side_effect=run):
        await dispatch.dispatch_message(agent, AsyncMock(), frame.to_json())

    (alarm,) = [
        e for e in events.buffer().drain("b") if e["kind"] == "whitelist_rejection"
    ]
    assert alarm["command_ref"] == frame.id


@pytest.mark.asyncio
async def test_a_sealed_hatch_with_a_revoked_grant_is_refused_as_sealed(
    agent: Agent,
) -> None:
    """Admission order is seal, then fence: the operator's seal wins, so a
    sealed hatch is a quiet ``signoff_sealed`` record, never a fence alarm."""
    agent.registry["run_verify_block"] = replace(
        agent.registry["run_verify_block"], authorize=lambda: "grant revoked"
    )
    agent.signoff_state.seal()
    events.buffer().drain("b")
    ws = AsyncMock()
    frame = sign_command_request(
        command="run_verify_block", params={"verify_command": "true"}
    )
    with patch("stormpulse.agent.dispatch.execute_command") as ex:
        await dispatch.dispatch_message(agent, ws, frame)

    ex.assert_not_called()
    (result,) = _wire_results(ws)
    assert result["failure_reason"] == "signoff_sealed"
    kinds = [e["kind"] for e in events.buffer().drain("b")]
    assert "whitelist_rejection" not in kinds
    assert kinds.count("sealed_refusal") == 1


@pytest.mark.asyncio
async def test_a_reseal_from_another_process_stops_the_next_hatch_step(
    agent: Agent,
) -> None:
    """The real reseal is ``stormpulse signoff seal`` in another process: a
    second ``SignoffState`` on the same directory, not the agent's own object."""
    ws = AsyncMock()

    def run(*_a: Any, **_k: Any) -> Any:
        SignoffState(agent.signoff_state.path.parent).seal()
        return make_successful_result()

    frame = signed_command_sequence(
        ["git_pull", "run_apply_block"], stop_on_failure=False
    )
    with patch("stormpulse.agent.dispatch.execute_command", side_effect=run) as ex:
        await dispatch.dispatch_message(agent, ws, frame.to_json())

    ex.assert_called_once()
    _, second = _wire_results(ws)
    assert (second["step_index"], second["failure_reason"]) == (1, "signoff_sealed")


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
    agent.shippers["grp"] = fake_shipper
    receipt = Receipt("grp", "/var/log/x.log", None, 0, 4242)
    agent.pending_batches.add("bid-1", receipt)

    env = envelope(MessageType.LOG_BATCH_ACK, {"batch_id": "bid-1"}, id="x")
    await acks.handle_log_batch_ack(agent, AsyncMock(), env)

    fake_shipper.confirm.assert_called_once_with(receipt)
    assert "bid-1" not in agent.pending_batches


@pytest.mark.asyncio
async def test_log_batch_ack_confirms_while_the_batch_is_still_pending(
    agent: Agent,
) -> None:
    """A read landing mid-confirm must still see the batch pending, or it reads as lost."""
    receipt = Receipt("grp", "c", None, "t0", "t1")
    seen: list[tuple[Receipt, ...]] = []
    fake_shipper = MagicMock()
    fake_shipper.confirm.side_effect = lambda _r: seen.append(
        agent.pending_batches.receipts()
    )
    agent.shippers["grp"] = fake_shipper
    agent.pending_batches.add("bid-1", receipt)

    env = envelope(MessageType.LOG_BATCH_ACK, {"batch_id": "bid-1"}, id="x")
    await acks.handle_log_batch_ack(agent, AsyncMock(), env)

    assert seen == [(receipt,)]
    assert "bid-1" not in agent.pending_batches


@pytest.mark.asyncio
async def test_log_batch_ack_for_a_group_with_no_shipper_resolves_the_batch(
    agent: Agent,
) -> None:
    agent.pending_batches.add("bid-1", Receipt("gone", "c", None, "t0", "t1"))
    env = envelope(MessageType.LOG_BATCH_ACK, {"batch_id": "bid-1"}, id="x")
    await acks.handle_log_batch_ack(agent, AsyncMock(), env)
    assert "bid-1" not in agent.pending_batches


@pytest.mark.asyncio
async def test_log_batch_ack_resolves_the_batch_even_when_confirm_raises(
    agent: Agent,
) -> None:
    fake_shipper = MagicMock()
    fake_shipper.confirm.side_effect = OSError("disk full")
    agent.shippers["grp"] = fake_shipper
    agent.pending_batches.add("bid-1", Receipt("grp", "c", None, "t0", "t1"))
    env = envelope(MessageType.LOG_BATCH_ACK, {"batch_id": "bid-1"}, id="x")
    with contextlib.suppress(OSError):
        await acks.handle_log_batch_ack(agent, AsyncMock(), env)
    assert "bid-1" not in agent.pending_batches


@pytest.mark.asyncio
async def test_log_batch_ack_confirms_on_the_receipts_own_group(
    agent: Agent, tmp_path: Path
) -> None:
    """Two groups ship; the ACK lands on the group its receipt names, not another."""
    store = LogPositionStore(tmp_path / "pos.db")
    tailers: dict[str, LogTailer] = {}
    for name in ("a", "b"):
        log = tmp_path / f"{name}.log"
        log.write_text("line\n")
        group = LogGroupConfig(
            name=name,
            enabled=True,
            source_type="file",
            source_path=log,
            filter_contains="",
            parser="stormpulse",
            ship_interval_seconds=10.0,
            max_lines_per_batch=50,
        )
        tailers[name] = LogTailer(group, store)
        agent.shippers[name] = LogShipper(group, tailers[name])
    _, receipt = tailers["b"].read_new_lines(max_lines=10)
    agent.pending_batches.add("bid-b", receipt)

    env = envelope(MessageType.LOG_BATCH_ACK, {"batch_id": "bid-b"}, id="x")
    await acks.handle_log_batch_ack(agent, AsyncMock(), env)

    try:
        assert store.get("b")[0] == len("line\n")
        assert store.get("a") == (0, None)
    finally:
        store.close()


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
        signoff_state=unsealed_state(config.storage.db_path.parent),
        pulse_logger=pulse_logger,
    )
    ws = AsyncMock()
    from tests.helpers import matched_test_session

    matched_test_session(ag)
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
    replace_runtime_state(
        rt, replace(make_fake_garage_state(), read=Freshness(changed_ids=("a" * 64,)))
    )
    track_push(ag.integrations, "push-1")
    merge_items_into_runtime(rt, [make_garage_bucket("b" * 64)])
    ws = AsyncMock()
    await dispatch.dispatch_message(
        ag, ws, envelope(MessageType.METRICS_ACK, {"push_id": "push-1"}).to_json()
    )
    assert list(rt.changed_revisions) == ["b" * 64]
    ws.send.assert_not_called()


@pytest.mark.asyncio
async def test_metrics_ack_for_an_unknown_duplicate_or_retired_push_releases_nothing(
    agent_with_garage: Callable[..., Agent], caplog: pytest.LogCaptureFixture
) -> None:
    """Three acks no push is waiting for: a push_id never sent, one acked already,
    one retired when its session ended. Each is a debug line, never an ``error``
    frame (an answered ack would ping-pong), and the dirty ids stay dirty."""
    ag = agent_with_garage()
    rt = ag.integrations["garage"]
    replace_runtime_state(
        rt, replace(make_fake_garage_state(), read=Freshness(changed_ids=("a" * 64,)))
    )
    ws = AsyncMock()

    async def ack(push_id: str) -> None:
        env = envelope(MessageType.METRICS_ACK, {"push_id": push_id})
        await dispatch.dispatch_message(ag, ws, env.to_json())

    track_push(ag.integrations, "push-1")
    with caplog.at_level(logging.DEBUG, logger="stormpulse.agent.acks"):
        await ack("never-sent")
        await ack("push-1")
        merge_items_into_runtime(rt, [make_garage_bucket("b" * 64)])
        await ack("push-1")  # duplicate: the newer read is not its to release
        track_push(ag.integrations, "push-2")
        discard_pending_pushes(ag.integrations)  # the session ended
        await ack("push-2")  # retired with it
    assert list(rt.changed_revisions) == ["b" * 64]
    ws.send.assert_not_called()
    unknown = [r for r in caplog.records if "unknown push_id" in r.getMessage()]
    assert [r.levelno for r in unknown] == [logging.DEBUG] * 3


@pytest.mark.asyncio
async def test_metrics_ack_with_an_empty_push_id_is_a_warning_and_releases_nothing(
    agent_with_garage: Callable[..., Agent], caplog: pytest.LogCaptureFixture
) -> None:
    ag = agent_with_garage()
    rt = ag.integrations["garage"]
    replace_runtime_state(
        rt, replace(make_fake_garage_state(), read=Freshness(changed_ids=("a" * 64,)))
    )
    track_push(ag.integrations, "push-1")
    ws = AsyncMock()
    with caplog.at_level(logging.DEBUG, logger="stormpulse.agent.acks"):
        await dispatch.dispatch_message(
            ag, ws, envelope(MessageType.METRICS_ACK, {"push_id": ""}).to_json()
        )
    assert list(rt.changed_revisions) == ["a" * 64]
    ws.send.assert_not_called()
    said = [r for r in caplog.records if "push_id" in r.getMessage()]
    assert [r.levelno for r in said] == [logging.WARNING]


# ---------------------------------------------------------------------------
# events.batch.ack
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_batch_ack_releases_only_the_named_batch(
    agent: Agent, caplog: pytest.LogCaptureFixture
) -> None:
    """An acked batch is gone for good: a reconnect re-ships nothing of it. An
    ack naming a batch never sent is a debug line and leaves the in-flight one
    for the reconnect to requeue. Neither is answered."""
    buf = events.buffer()
    buf.append({"kind": "a"})
    buf.drain("b1")
    ws = AsyncMock()

    async def ack(batch_id: str) -> None:
        env = envelope(MessageType.EVENTS_BATCH_ACK, {"batch_id": batch_id})
        await dispatch.dispatch_message(agent, ws, env.to_json())

    with caplog.at_level(logging.DEBUG, logger="stormpulse.agent.acks"):
        await ack("never-sent")
    said = [r for r in caplog.records if "never-sent" in r.getMessage()]
    assert [r.levelno for r in said] == [logging.DEBUG]
    assert buf.requeue_unacked() == 1
    buf.drain("b2")
    await ack("b2")
    assert buf.requeue_unacked() == 0
    assert buf.drain("b3") == []
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


# ---------------------------------------------------------------------------
# No job request resolves silently (CORE-011 d7, never-again)
# ---------------------------------------------------------------------------


def _job_spec(unblock: asyncio.Event) -> CommandSpec:
    """A job whose handler parks on *unblock*, so the test decides when it ends."""

    async def run(progress: Any) -> JobOutcome:
        await unblock.wait()
        return JobOutcome(success=True)

    return CommandSpec(
        group="g", command=["some_job"], timeout=5, mode="job", handler=lambda p: run
    )


def _job_request(request_id: str) -> str:
    """A signed ``some_job`` frame with a chosen id, so a reply's echo can be checked."""
    body = {"command": "some_job", "params": {}, "nonce": f"n-{request_id}"}
    return signed(envelope(MessageType.COMMAND_REQUEST, body, id=request_id)).to_json()


@pytest.mark.asyncio
async def test_job_before_any_session_is_a_terminal_session_closing_result(
    agent: Agent,
) -> None:
    """No JobManager yet: the request is answered over the socket, never dropped."""
    agent.registry["some_job"] = _job_spec(asyncio.Event())
    assert agent.job_manager is None
    ws = AsyncMock()
    await dispatch.dispatch_message(agent, ws, _job_request("before-session"))
    ws.send.assert_called_once()
    sent = json.loads(ws.send.call_args.args[0])
    assert sent["type"] == "command.result"
    assert sent["payload"]["success"] is False
    assert sent["payload"]["failure_reason"] == "session_closing"
    assert sent["payload"]["stderr"].endswith("Retry after the agent reconnects.")
    # The echo is what resolves the request on the far end; a blank one is silence.
    echo = {k: sent["payload"][k] for k in ("request_id", "command", "group")}
    assert echo == {"request_id": "before-session", "command": "some_job", "group": "g"}


@pytest.mark.asyncio
async def test_job_refused_by_a_closing_manager_is_a_terminal_result(
    agent: Agent,
) -> None:
    """``start`` is the one admission gate: a manager that closed admission
    answers with a ``session_closing`` result through its own send (d7)."""
    agent.registry["some_job"] = _job_spec(asyncio.Event())
    sent: list[Any] = []

    async def send(env: Any) -> None:
        sent.append(env)

    manager = JobManager(AGENT_ID, send)
    await manager.shutdown_all(CancelReason.SHUTDOWN)
    assert manager.closing and sent == []
    agent.job_manager = manager
    ws = AsyncMock()
    await dispatch.dispatch_message(agent, ws, _job_request("while-closing"))
    ws.send.assert_not_called()
    assert [env.type for env in sent] == [MessageType.COMMAND_RESULT]
    assert sent[0].payload["success"] is False
    assert sent[0].payload["failure_reason"] == "session_closing"
    assert sent[0].payload["stderr"].endswith("Retry after the agent reconnects.")
    echo = {k: sent[0].payload[k] for k in ("request_id", "command", "group")}
    assert echo == {"request_id": "while-closing", "command": "some_job", "group": "g"}
    assert not manager.is_running("while-closing")


@pytest.mark.asyncio
async def test_duplicate_request_is_an_error_and_the_first_job_keeps_running(
    agent: Agent,
) -> None:
    """A second frame with a live request id gets an ``error`` naming the reason
    and type only (CORE-011 d2); the first job runs on and resolves once."""
    unblock = asyncio.Event()
    agent.registry["some_job"] = _job_spec(unblock)
    sent: list[Any] = []

    async def send(env: Any) -> None:
        sent.append(env)

    manager = JobManager(AGENT_ID, send)
    agent.job_manager = manager
    ws = AsyncMock()
    frames = [
        signed(
            envelope(
                MessageType.COMMAND_REQUEST,
                {"command": "some_job", "params": {}, "nonce": f"n{i}"},
                id="same-id",
            )
        ).to_json()
        for i in range(2)
    ]
    for frame in frames:
        await dispatch.dispatch_message(agent, ws, frame)
    reply = _only_error_reply(ws)
    assert reply["payload"] == {"error": "duplicate_request", "type": "command.request"}
    assert reply["payload"]["error"] in ProtocolError.REASONS
    assert manager.is_running("same-id")
    assert sent == []

    unblock.set()
    for _ in range(100):
        if not manager.is_running("same-id"):
            break
        await asyncio.sleep(0)
    results = [e for e in sent if e.type is MessageType.COMMAND_RESULT]
    assert [(e.payload["request_id"], e.payload["success"]) for e in results] == [
        ("same-id", True)
    ]


@pytest.mark.asyncio
async def test_a_job_s_retry_window_reaches_the_manager(agent: Agent) -> None:
    """CORE-012 d3: the window is declared on the spec and spent by the manager;
    dispatch is the hop between them, so dropping it here disables every retry."""
    unblock = asyncio.Event()
    agent.registry["some_job"] = replace(_job_spec(unblock), retry_window_s=45)
    manager = JobManager(AGENT_ID, AsyncMock())
    agent.job_manager = manager
    with patch.object(manager, "start", wraps=manager.start) as start:
        await dispatch.dispatch_message(agent, AsyncMock(), _job_request("windowed"))
    assert start.call_args.kwargs["retry_window_s"] == 45
    unblock.set()
    await asyncio.gather(*manager._jobs.values(), return_exceptions=True)
