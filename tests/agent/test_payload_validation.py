"""Malformed authenticated input is refused before any command runs."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from stormpulse import events
from stormpulse.agent import Agent, dispatch
from stormpulse.auth import sign_envelope
from stormpulse.protocol import MessageType
from tests.helpers import SECRET, envelope


def signed_frame(msg_type: MessageType, payload: dict[str, Any]) -> str:
    frame = envelope(msg_type, payload).to_dict()
    frame["payload"]["hmac"] = sign_envelope(frame, SECRET)
    return json.dumps(frame)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("command", []),
        ("command", 42),
        ("params", 42),
        ("params", []),
        ("params", None),
        ("params", False),
        ("nonce", []),
        ("nonce", 42),
    ],
)
async def test_signed_invalid_request_never_executes_or_stores_nonce(
    agent: Agent,
    field: str,
    value: Any,
) -> None:
    payload = {"command": "git_pull", "params": {}, "nonce": "n", field: value}
    ws = AsyncMock()
    with (
        patch.object(agent._nonce_store, "check_and_store") as store,
        patch.object(
            dispatch, "_run_subprocess_command", new_callable=AsyncMock
        ) as execute,
    ):
        await dispatch.dispatch_message(
            agent, ws, signed_frame(MessageType.COMMAND_REQUEST, payload)
        )
    execute.assert_not_called()
    store.assert_not_called()
    reply = json.loads(ws.send.call_args.args[0])
    assert reply["type"] == "error"
    assert reply["payload"] == {"error": "bad_envelope", "type": "command.request"}


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["false", "true", 0, 1, None, [], {}])
async def test_sequence_control_requires_a_boolean(agent: Agent, control: Any) -> None:
    payload = {
        "sequence_id": "s",
        "steps": [{"command": "git_pull", "params": {}}],
        "stop_on_failure": control,
        "nonce": "n",
    }
    ws = AsyncMock()
    with patch.object(dispatch, "execute_command") as execute:
        await dispatch.dispatch_message(
            agent, ws, signed_frame(MessageType.COMMAND_SEQUENCE, payload)
        )
    execute.assert_not_called()
    reply = json.loads(ws.send.call_args.args[0])
    assert reply["payload"] == {"error": "bad_envelope", "type": "command.sequence"}


@pytest.mark.asyncio
async def test_malformed_later_step_is_refused_before_the_first_step(
    agent: Agent,
) -> None:
    payload = {
        "sequence_id": "s",
        "steps": [
            {"command": "git_pull", "params": {}},
            {"command": "git_pull", "params": 42},
        ],
        "stop_on_failure": False,
        "nonce": "n",
    }
    ws = AsyncMock()
    with patch.object(dispatch, "execute_command") as execute:
        await dispatch.dispatch_message(
            agent, ws, signed_frame(MessageType.COMMAND_SEQUENCE, payload)
        )
    execute.assert_not_called()
    assert json.loads(ws.send.call_args.args[0])["payload"]["error"] == "bad_envelope"


@pytest.mark.asyncio
@pytest.mark.parametrize("hmac_value", ["é", "x" * 64, "0" * 63, 42, None])
async def test_bad_hmac_is_a_witnessed_auth_refusal(
    agent: Agent, hmac_value: Any
) -> None:
    ws = AsyncMock()
    frame = envelope(
        MessageType.COMMAND_REQUEST,
        {
            "command": [],
            "params": 42,
            "nonce": [],
            "hmac": hmac_value,
        },
    )
    with patch.object(
        dispatch, "_run_subprocess_command", new_callable=AsyncMock
    ) as execute:
        await dispatch.dispatch_message(agent, ws, frame.to_json())
    execute.assert_not_called()
    ws.send.assert_not_called()
    (event,) = events.buffer().drain("audit")
    assert event["kind"] == "signature_failure"
    assert event["failure_reason"] == "bad_signature"


@pytest.mark.asyncio
async def test_payload_refusal_does_not_block_the_next_message(agent: Agent) -> None:
    malformed = signed_frame(
        MessageType.COMMAND_REQUEST,
        {
            "command": "git_pull",
            "params": {},
            "nonce": [],
        },
    )
    messages = iter([malformed, envelope(MessageType.HEARTBEAT_ACK, {}).to_json()])
    reads = 0

    async def receive() -> str:
        nonlocal reads
        reads += 1
        if reads == 2:
            agent.shutdown.set()
        return next(messages)

    ws = MagicMock(recv=AsyncMock(side_effect=receive), send=AsyncMock())
    await dispatch.receive_loop(agent, ws)
    assert ws.recv.await_count == 2
    assert ws.send.await_count == 1
    assert json.loads(ws.send.call_args.args[0])["payload"]["error"] == "bad_envelope"
