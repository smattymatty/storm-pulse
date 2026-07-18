"""Tests for the inbound message dispatcher."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from stormpulse.agent import Agent, dispatch
from stormpulse.auth import NonceStore
from stormpulse.config import Config
from stormpulse.protocol import Envelope, MessageType, make_heartbeat
from stormpulse.signoff import SignoffState
from tests.helpers import (
    AGENT_ID,
    SECRET,
    make_failed_result,
    make_successful_result,
    sign_command_request,
    sign_command_sequence,
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


@pytest.mark.asyncio
async def test_dispatch_bad_json(agent: Agent) -> None:
    ws = AsyncMock()
    await dispatch.dispatch_message(agent, ws, "not json{{{")
    ws.send.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_bad_hmac(agent: Agent) -> None:
    ws = AsyncMock()
    envelope = Envelope(
        v=1,
        type=MessageType.COMMAND_REQUEST,
        id=str(uuid.uuid4()),
        ts=datetime.now(UTC),
        agent_id=AGENT_ID,
        payload={"command": "git_pull", "params": {}, "hmac": "bad", "nonce": "n"},
    )
    await dispatch.dispatch_message(agent, ws, envelope.to_json())
    ws.send.assert_not_called()


@pytest.mark.asyncio
async def test_dispatch_unexpected_type(agent: Agent) -> None:
    ws = AsyncMock()
    heartbeat = make_heartbeat(AGENT_ID)
    await dispatch.dispatch_message(agent, ws, heartbeat.to_json())
    ws.send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ack_type",
    [
        MessageType.REGISTER_OK,
        MessageType.HEARTBEAT_ACK,
        MessageType.METRICS_ACK,
        MessageType.COMMAND_RESULT_ACK,
        MessageType.ERROR,
    ],
)
async def test_dispatch_ack_types_ignored(
    agent: Agent,
    ack_type: MessageType,
) -> None:
    ws = AsyncMock()
    envelope = Envelope(
        v=1,
        type=ack_type,
        id=str(uuid.uuid4()),
        ts=datetime.now(UTC),
        agent_id=AGENT_ID,
        payload={},
    )
    await dispatch.dispatch_message(agent, ws, envelope.to_json())
    ws.send.assert_not_called()


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
    assert sent_data["payload"]["failure_reason"] == "validation_failed"
    assert sent_data["payload"]["command"] == "this_command_does_not_exist"
    assert sent_data["payload"]["sequence_id"]


@pytest.mark.asyncio
async def test_dispatch_unknown_command_sends_failure(agent: Agent) -> None:
    """An authenticated request for an unknown command gets a wire failure."""
    ws = AsyncMock()
    await dispatch.dispatch_message(
        agent, ws, sign_command_request(command="this_command_does_not_exist")
    )
    ws.send.assert_called_once()
    sent_data = json.loads(ws.send.call_args[0][0])
    assert sent_data["type"] == "command.result"
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
# log.batch.ack
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_log_batch_ack_missing_batch_id(agent: Agent) -> None:
    envelope = Envelope(
        v=1,
        type=MessageType.LOG_BATCH_ACK,
        id="x",
        ts=datetime.now(UTC),
        agent_id=AGENT_ID,
        payload={},
    )
    # Should not raise
    await dispatch.handle_log_batch_ack(agent, envelope)


@pytest.mark.asyncio
async def test_log_batch_ack_unknown_batch_is_noop(agent: Agent) -> None:
    envelope = Envelope(
        v=1,
        type=MessageType.LOG_BATCH_ACK,
        id="x",
        ts=datetime.now(UTC),
        agent_id=AGENT_ID,
        payload={"batch_id": "never-sent"},
    )
    await dispatch.handle_log_batch_ack(agent, envelope)


@pytest.mark.asyncio
async def test_log_batch_ack_advances_position(agent: Agent) -> None:
    fake_shipper = MagicMock()
    fake_shipper.tailer.confirm_shipped = MagicMock()
    agent.shippers["grp"] = fake_shipper
    agent.pending_batches.add("bid-1", "grp", 4242)

    envelope = Envelope(
        v=1,
        type=MessageType.LOG_BATCH_ACK,
        id="x",
        ts=datetime.now(UTC),
        agent_id=AGENT_ID,
        payload={"batch_id": "bid-1"},
    )
    await dispatch.handle_log_batch_ack(agent, envelope)

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
