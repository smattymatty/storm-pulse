"""Inbound message dispatch: auth-verify, seal-check, route to inline / long-running / subprocess paths."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING, TypeVar

from websockets.asyncio.client import ClientConnection

from stormpulse.agent.garage_actions import (
    handle_garage_refresh,
    post_success_hook,
)
from stormpulse.agent.long_running import resolve_long_running_handler
from stormpulse.agent.signoff_guard import (
    SEALED_COMMANDS,
    is_blocked_by_seal,
    sealed_refusal_result,
)
from stormpulse.auth import AuthError, verify_envelope
from stormpulse.commands import (
    CommandError,
    ParamValidationError,
    execute_command,
    get_command,
)
from stormpulse.commands.registry import validate_params
from stormpulse.config import CommandDef
from stormpulse.protocol import (
    CommandRequestPayload,
    CommandResultPayload,
    CommandSequencePayload,
    Envelope,
    MessageType,
    ProtocolError,
    make_command_result,
)

if TYPE_CHECKING:
    from stormpulse.agent import Agent

logger = logging.getLogger(__name__)

# Dashboard acknowledgement types, received but not actionable.
# SIGNOFF_STATE_ACK belongs in this set once the signoff.state envelope
# lands. The enum value is not yet defined in protocol.py on main, so
# referencing it here crash-loops the agent at import. Re-add it in the
# same commit as the protocol.py enum addition.
ACK_TYPES = frozenset(
    {
        MessageType.REGISTER_OK,
        MessageType.HEARTBEAT_ACK,
        MessageType.METRICS_ACK,
        MessageType.COMMAND_RESULT_ACK,
        MessageType.ERROR,
    }
)


async def receive_loop(agent: Agent, ws: ClientConnection) -> None:
    """Receive and dispatch inbound messages.

    A malformed or buggy message must not kill the receive loop — the
    agent drops it and keeps serving. Auth failures already return
    early without executing anything, so swallowing here cannot cause
    an unauthorized command to run; worst case is a logged bug.
    """
    while not agent.shutdown.is_set():
        message = await ws.recv()
        try:
            await dispatch_message(agent, ws, message)
        except Exception:
            logger.warning("Error dispatching message", exc_info=True)


async def dispatch_message(
    agent: Agent,
    ws: ClientConnection,
    raw: str | bytes,
) -> None:
    """Parse envelope, verify auth, execute command(s)."""
    try:
        envelope = Envelope.from_json(raw)
    except ProtocolError as exc:
        logger.warning("Invalid message: %s", exc)
        return

    if envelope.type in ACK_TYPES:
        logger.debug("Received %s (%s)", envelope.type.value, envelope.id)
        return

    match envelope.type:
        case MessageType.COMMAND_REQUEST:
            await handle_command_request(agent, ws, envelope)
        case MessageType.COMMAND_SEQUENCE:
            await handle_command_sequence(agent, ws, envelope)
        case MessageType.LOG_BATCH_ACK:
            await handle_log_batch_ack(agent, envelope)
        case _:
            logger.warning("Unexpected message type: %s", envelope.type.value)


async def handle_log_batch_ack(agent: Agent, envelope: Envelope) -> None:
    """Advance the stored log position for an acknowledged batch."""
    batch_id = envelope.payload.get("batch_id")
    if not isinstance(batch_id, str):
        logger.warning("log.batch.ack missing batch_id")
        return
    pending = agent.pending_batches.pop(batch_id)
    if pending is None:
        logger.debug("log.batch.ack for unknown batch_id %s", batch_id)
        return
    group_name, to_position = pending
    shipper = agent.shippers.get(group_name)
    if shipper is None:
        return
    await asyncio.to_thread(shipper.tailer.confirm_shipped, to_position)  # type: ignore[arg-type]
    logger.debug(
        "Advanced position for group %s to %s (batch %s)",
        group_name,
        to_position,
        batch_id,
    )


async def handle_command_request(
    agent: Agent,
    ws: ClientConnection,
    envelope: Envelope,
) -> None:
    """Verify and execute a single command request.

    Three execution paths, each owned end-to-end by its handler:
    ``garage_refresh`` runs inline (collect + result + immediate
    metrics push, all inside ``handle_garage_refresh``); long-running
    commands hand off to ``JobManager`` via ``dispatch_long_running``;
    everything else runs as a one-shot subprocess via
    ``execute_command`` with the result-send epilogue handled here.
    """
    payload = _verify_typed_payload(agent, envelope, CommandRequestPayload)
    if payload is None:
        return
    logger.info("Executing command %r (request %s)", payload.command, envelope.id)
    cmd_def = agent.registry.get(payload.command)

    if await _refuse_if_sealed(agent, ws, envelope, payload, cmd_def):
        return

    if payload.command == "garage_refresh":
        await handle_garage_refresh(agent, ws, envelope.id)
        return
    if cmd_def is not None and cmd_def.long_running:
        await dispatch_long_running(agent, envelope.id, payload, cmd_def)
        return

    result = await _run_subprocess_command(agent, payload, envelope.id)
    if result is None:
        return
    await _send_result(agent, ws, result)
    _log_to_pulse(agent, payload.command, result)


_PayloadT = TypeVar("_PayloadT")


def _verify_typed_payload(
    agent: Agent,
    envelope: Envelope,
    expected_type: type[_PayloadT],
) -> _PayloadT | None:
    """Verify envelope auth and assert payload type, or return ``None`` to drop."""
    try:
        payload = verify_envelope(
            envelope,
            agent._secret,
            agent._nonce_store,
            agent.config.auth.command_max_age_seconds,
        )
    except AuthError as exc:
        logger.warning("Auth failed for %s: %s", envelope.id, exc)
        return None
    if not isinstance(payload, expected_type):
        logger.error(
            "Expected %s, got %s", expected_type.__name__, type(payload).__name__
        )
        return None
    return payload


async def _refuse_if_sealed(
    agent: Agent,
    ws: ClientConnection,
    envelope: Envelope,
    payload: CommandRequestPayload,
    cmd_def: CommandDef | None,
) -> bool:
    """Refuse a sealed verify or apply block inline. Returns ``True`` if handled.

    Dispatch-time recheck catches the case where the operator ran
    ``stormpulse signoff seal`` after the registry was built: the
    registry still contains the seal-gated commands, but the seal file
    now says "no". See ADR CORE-004.
    """
    if not is_blocked_by_seal(agent.signoff_state, [payload.command]):
        return False
    sealed = sealed_refusal_result(envelope.id, payload.command, cmd_def)
    await ws.send(make_command_result(agent.config.agent.id, sealed).to_json())
    logger.warning(
        "Refused %s (request %s): signoff is sealed",
        payload.command,
        envelope.id,
    )
    return True


async def _run_subprocess_command(
    agent: Agent,
    payload: CommandRequestPayload,
    request_id: str,
) -> CommandResultPayload | None:
    """Run a whitelisted subprocess command, or return ``None`` on dispatch error.

    A ``None`` return means a registry / validation error was logged
    and the dispatcher should drop the request — never silently send a
    misleading success result.
    """
    try:
        return await asyncio.to_thread(
            execute_command,
            payload.command,
            agent.config.project,
            request_id,
            registry=agent.registry,
            runtime_params=payload.params or None,
        )
    except (CommandError, ParamValidationError) as exc:
        logger.warning("Command error for %s: %s", request_id, exc)
        return None


async def _send_result(
    agent: Agent,
    ws: ClientConnection,
    result: CommandResultPayload,
) -> None:
    """Send a ``command.result`` envelope and log the outcome."""
    response = make_command_result(agent.config.agent.id, result)
    await ws.send(response.to_json())
    logger.info(
        "Sent result for %r: success=%s, %dms",
        result.command,
        result.success,
        result.duration_ms,
    )


def _log_to_pulse(
    agent: Agent,
    command: str,
    result: CommandResultPayload,
) -> None:
    """Mirror a command result to the PulseLogger (if configured)."""
    if agent.pulse_logger is None:
        return
    cmd_def = agent.registry.get(command)
    sensitive = cmd_def.sensitive_output if cmd_def else False
    agent.pulse_logger.log_command_result(
        command=result.command,
        success=result.success,
        duration_ms=result.duration_ms,
        sensitive=sensitive,
    )


async def dispatch_long_running(
    agent: Agent,
    request_id: str,
    payload: CommandRequestPayload,
    cmd_def: CommandDef,
) -> None:
    """Hand a long-running command off to the JobManager.

    Sends a synthetic failure result inline if the command is marked
    ``long_running`` but no internal handler is registered (config bug)
    or the connection is no longer active.
    """
    if agent.job_manager is None:
        logger.error("Cannot dispatch %r: no active JobManager", payload.command)
        return

    # Enforce the same param-validation contract the subprocess path
    # uses. CommandDef declares regex patterns for every param;
    # honoring them here closes the asymmetry between the two dispatch
    # paths and prevents malformed values (e.g. a bucket name with path
    # traversal characters, or an s3_endpoint pointing somewhere
    # unintended) from reaching the handler.
    try:
        validated_params = validate_params(cmd_def, payload.params or {})
    except ParamValidationError as exc:
        logger.warning("Param validation failed for %s: %s", request_id, exc)
        failure = CommandResultPayload(
            request_id=request_id,
            command=payload.command,
            group=cmd_def.group,
            success=False,
            exit_code=-1,
            stdout="",
            stderr=str(exc),
            duration_ms=0,
            failure_reason="os_error",
        )
        await agent.job_manager.send_now(
            make_command_result(agent.config.agent.id, failure)
        )
        return

    handler = resolve_long_running_handler(
        agent.long_running_factories,
        payload.command,
        validated_params,
    )
    if handler is None:
        logger.error(
            "Command %r is marked long_running but no handler is registered",
            payload.command,
        )
        failure = CommandResultPayload(
            request_id=request_id,
            command=payload.command,
            group=cmd_def.group,
            success=False,
            exit_code=-1,
            stdout="",
            stderr=f"No long-running handler for {payload.command!r}",
            duration_ms=0,
            failure_reason="os_error",
        )
        await agent.job_manager.send_now(
            make_command_result(agent.config.agent.id, failure)
        )
        return

    on_success = post_success_hook(agent, cmd_def, payload.command)
    try:
        agent.job_manager.start(
            request_id,
            payload.command,
            cmd_def.group,
            handler,
            on_success=on_success,
        )
    except ValueError:
        logger.warning("Duplicate dispatch for request %s", request_id)


async def handle_command_sequence(
    agent: Agent,
    ws: ClientConnection,
    envelope: Envelope,
) -> None:
    """Verify and execute a command sequence, streaming results."""
    payload = _verify_typed_payload(agent, envelope, CommandSequencePayload)
    if payload is None:
        return
    logger.info(
        "Executing sequence %s: %s",
        payload.sequence_id,
        payload.commands,
    )

    try:
        for name in payload.commands:
            get_command(name, registry=agent.registry)
    except CommandError as exc:
        logger.warning("Sequence %s has invalid command: %s", payload.sequence_id, exc)
        return

    # Same dispatch-time seal recheck as the single-command path: refuse
    # the sequence pre-flight if any step is a seal-gated command and
    # the agent was sealed since the registry was built.
    if is_blocked_by_seal(agent.signoff_state, payload.commands):
        sealed_in_sequence = sorted(SEALED_COMMANDS & set(payload.commands))
        logger.warning(
            "Refused sequence %s: contains %s while sealed",
            payload.sequence_id,
            ", ".join(sealed_in_sequence),
        )
        return

    agent_id = agent.config.agent.id
    project = agent.config.project
    for name in payload.commands:
        request_id = str(uuid.uuid4())
        result = await asyncio.to_thread(
            execute_command,
            name,
            project,
            request_id,
            payload.sequence_id,
            registry=agent.registry,
        )
        response = make_command_result(agent_id, result)
        await ws.send(response.to_json())
        logger.info(
            "Sequence %s step %r: success=%s, %dms",
            payload.sequence_id,
            name,
            result.success,
            result.duration_ms,
        )
        if payload.stop_on_failure and not result.success:
            logger.warning(
                "Sequence %s halted at %r",
                payload.sequence_id,
                name,
            )
            break
