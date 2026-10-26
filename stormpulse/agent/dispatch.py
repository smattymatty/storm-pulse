"""Inbound message dispatch: auth-verify, seal-check, route to inline / long-running / subprocess paths."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from websockets.asyncio.client import ClientConnection

from stormpulse import events
from stormpulse.agent.acks import (
    handle_ack,
    handle_error,
    handle_events_batch_ack,
    handle_events_batch_nack,
    handle_log_batch_ack,
    handle_metrics_ack,
)
from stormpulse.agent.integrations_runtime import (
    STATUS_LIVE,
    build_metrics_envelope,
    discard_push,
    merge_items_into_runtime,
)
from stormpulse.agent.refresh import handle_refresh
from stormpulse.agent.signoff_guard import (
    SEALED_COMMANDS,
    is_blocked_by_seal,
    needs_restart_to_load,
    restart_pending_result,
    sealed_refusal_result,
)
from stormpulse.auth import AuthError, verify_envelope
from stormpulse.commands import (
    CommandError,
    ParamValidationError,
    execute_command,
    get_command,
)
from stormpulse.commands.jobs import JobManagerClosing
from stormpulse.commands.registry import non_secret_params, validate_params
from stormpulse.config import CommandSpec
from stormpulse.protocol import (
    MESSAGES,
    AckPayload,
    CommandRequestPayload,
    CommandResultPayload,
    CommandSequencePayload,
    Envelope,
    FailureReason,
    MessageType,
    ProtocolError,
    ResultError,
    make,
    make_error,
)

if TYPE_CHECKING:
    from stormpulse.agent import Agent

logger = logging.getLogger(__name__)
events.declare_event("auth", "signature_failure", {"status", "failure_reason", "error"})
events.declare_event("commands", "whitelist_rejection", {"status", "command", "error"})

# The group a result reports when the registry holds no spec for the name.
_UNKNOWN_GROUP = "unknown"

type Handler = Callable[[Agent, ClientConnection, Envelope], Awaitable[None]]


def _sequence_position(
    payload: CommandRequestPayload | CommandSequencePayload,
) -> tuple[str, str | None, int | None]:
    """The refused command's name and, for a sequence, its id and first step index."""
    if isinstance(payload, CommandRequestPayload):
        return payload.command, None, None
    if payload.steps:
        return payload.steps[0].command, payload.sequence_id, 0
    return "command.sequence", payload.sequence_id, None


async def refuse_incompatible_command(
    agent: Agent,
    ws: ClientConnection,
    envelope: Envelope,
    payload: CommandRequestPayload | CommandSequencePayload,
) -> bool:
    session = agent.session_compatibility
    if session is not None and session.compatible:
        return False
    message = (
        session.guidance
        if session is not None
        else (
            "Registration hasn't finished, so commands are paused. "
            "Wait for Storm Pulse to register with the control plane, then retry."
        )
    )
    command, sequence_id, step_index = _sequence_position(payload)
    spec = agent.registry.get(command)
    result = CommandResultPayload(
        request_id=envelope.id,
        command=command,
        group=spec.group if spec else _UNKNOWN_GROUP,
        success=False,
        exit_code=-1,
        stdout="",
        stderr=message,
        duration_ms=0,
        failure_reason=FailureReason.FORBIDDEN,
        error=ResultError(
            "contract_mismatch" if session is not None else "registration_pending",
            None,
            None,
            message,
        ),
        sequence_id=sequence_id,
        step_index=step_index,
    )
    await ws.send(make(agent.config.agent.id, result).to_json())
    return True


async def receive_loop(agent: Agent, ws: ClientConnection) -> None:
    """Receive and dispatch inbound messages; swallow per-message errors so the loop stays up."""
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
    """Parse envelope, verify auth, execute command(s); a refused frame is answered, never dropped."""
    envelope = None
    try:
        envelope = Envelope.from_json(raw)
        handler = HANDLERS.get(envelope.type)
        if handler is None:
            raise ProtocolError(
                f"Outbound message type sent to the agent: {envelope.type.value}",
                reason="unknown_type",
                msg_type=envelope.type.value,
            )
        await handler(agent, ws, envelope)
    except ProtocolError as exc:
        # CORE-011 d2: one `error` frame naming the reason and the type, the socket kept.
        logger.warning("Refused message (%s): %s", exc.reason, exc)
        await ws.send(
            make_error(
                agent.config.agent.id,
                exc.reason,
                exc.msg_type or (envelope.type.value if envelope is not None else None),
            ).to_json()
        )
        return


async def handle_command_request(
    agent: Agent,
    ws: ClientConnection,
    envelope: Envelope,
) -> None:
    """Verify and execute a single command request. Three paths, routed on spec.mode: refresh, job (long-running), subprocess."""
    payload = _verify_typed_payload(agent, envelope, CommandRequestPayload)
    if payload is None:
        return
    if await refuse_incompatible_command(agent, ws, envelope, payload):
        return
    logger.info("Executing command %r (request %s)", payload.command, envelope.id)
    cmd_def = agent.registry.get(payload.command)

    if await _refuse_if_sealed(agent, ws, envelope, payload, cmd_def):
        return

    if await _refuse_if_fenced(agent, ws, envelope, payload, cmd_def):
        return

    if await _deflect_if_restart_pending(agent, ws, envelope, payload):
        return

    if cmd_def is not None and cmd_def.mode == "refresh":
        # group == id (bootstrap-enforced): the spec's group names its integration.
        await handle_refresh(agent, ws, payload.command, envelope.id, cmd_def.group)
        return
    if cmd_def is not None and cmd_def.mode == "job":
        await dispatch_long_running(agent, ws, envelope.id, payload, cmd_def)
        return

    result = await _run_subprocess_command(agent, payload, envelope.id)
    await _send_result(agent, ws, result)
    _log_to_pulse(agent, payload.command, result)


def _verify_typed_payload[PayloadT](
    agent: Agent,
    envelope: Envelope,
    expected_type: type[PayloadT],
) -> PayloadT | None:
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
        # Dropping a refused envelope on the wire is correct: a forgery
        # gets no answer. Dropping it from the record is not. This emit
        # is the only witness that someone tried, and the agent is the
        # only place that can see it. The envelope id is untrusted here
        # (nothing about this message verified), so it is capped before
        # it rides the wire as an opaque ref.
        events.emit(
            "signature_failure",
            source="auth",
            status="refused",
            failure_reason=exc.reason,
            command_ref=str(envelope.id)[:100],
            error=str(exc),
        )
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
    cmd_def: CommandSpec | None,
) -> bool:
    """Refuse a sealed verify/apply block inline. Returns ``True`` if handled (ADR CORE-004)."""
    if not is_blocked_by_seal(agent.signoff_state, [payload.command]):
        return False
    sealed = sealed_refusal_result(envelope.id, payload.command, cmd_def)
    await ws.send(make(agent.config.agent.id, sealed).to_json())
    logger.warning(
        "Refused %s (request %s): signoff is sealed",
        payload.command,
        envelope.id,
    )
    return True


async def _refuse_if_fenced(
    agent: Agent,
    ws: ClientConnection,
    envelope: Envelope,
    payload: CommandRequestPayload,
    cmd_def: CommandSpec | None,
) -> bool:
    """Refuse, in every mode, a command its current grant no longer authorizes
    (CORE-007 D3). Returns ``True`` if handled."""
    if cmd_def is None or cmd_def.authorize is None:
        return False
    refused = cmd_def.authorize()
    if refused is None:
        return False
    message = f"Command {payload.command!r} is fenced: {refused}"
    logger.warning("Refused %s (request %s): %s", payload.command, envelope.id, refused)
    _emit_whitelist_rejection(payload.command, envelope.id, message)
    failure = CommandResultPayload.failed(
        envelope.id, payload.command, cmd_def.group, FailureReason.FORBIDDEN, message
    )
    await ws.send(make(agent.config.agent.id, failure).to_json())
    return True


def _emit_whitelist_rejection(command: str, ref: str, error: str) -> None:
    """The alarm for a command the whitelist refused: unknown, or fenced (D3)."""
    events.emit(
        "whitelist_rejection",
        source="commands",
        status="refused",
        command=str(command)[:100],
        command_ref=str(ref)[:100],
        error=error,
    )


async def _deflect_if_restart_pending(
    agent: Agent,
    ws: ClientConnection,
    envelope: Envelope,
    payload: CommandRequestPayload,
) -> bool:
    """Deflect a now-unsealed hatch command the boot-built registry has not loaded yet.

    Reply asking for a restart instead of letting it fall through to the
    whitelist-violation path, which would fire a security alarm for the
    operator's own legitimate command (ADR CORE-004). Returns ``True`` if handled.
    """
    restart_cmd = needs_restart_to_load(
        agent.signoff_state, agent.registry, [payload.command]
    )
    if restart_cmd is None:
        return False
    result = restart_pending_result(envelope.id, restart_cmd)
    await ws.send(make(agent.config.agent.id, result).to_json())
    logger.warning(
        "Deflected %s (request %s): unsealed after boot, registry not rebuilt "
        "(restart required)",
        payload.command,
        envelope.id,
    )
    return True


async def _run_subprocess_command(
    agent: Agent,
    payload: CommandRequestPayload,
    request_id: str,
) -> CommandResultPayload:
    """Run a whitelisted subprocess command; dispatch errors become failure results.

    The request already passed HMAC verification (auth failures drop silently
    upstream, in ``_verify_typed_payload``), so an unknown command or a bad
    param gets an honest failure on the wire, never silence: a dropped result
    is a dashboard spinner timing out with no reason.
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
        # A CommandError here means the name was not in the registry: the
        # agent was asked to run something outside its whitelist, which is
        # the invariant that makes this fleet auditable. That is a security
        # signal and it alarms. A ParamValidationError is an ordinary bad
        # request against a command that does exist, so it stays quiet.
        if isinstance(exc, CommandError):
            _emit_whitelist_rejection(payload.command, request_id, str(exc))
        cmd_def = agent.registry.get(payload.command)
        return CommandResultPayload.failed(
            request_id,
            payload.command,
            cmd_def.group if cmd_def else _UNKNOWN_GROUP,
            exc.reason
            if isinstance(exc, CommandError)
            else FailureReason.VALIDATION_FAILED,
            str(exc),
        )


async def _send_result(
    agent: Agent,
    ws: ClientConnection,
    result: CommandResultPayload,
) -> None:
    """Send a ``command.result`` envelope and log the outcome."""
    response = make(agent.config.agent.id, result)
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


def post_success_hook(
    agent: Agent,
    cmd_def: CommandSpec,
    command: str,
    params: Mapping[str, Any],
) -> Callable[[], Awaitable[None]] | None:
    """Build the after-success callback (targeted re-read, merge, push) for a mutating
    integration command, or ``None``. Owner resolves via group == id (CORE-005 d11/12)."""
    runtime = agent.integrations.get(cmd_def.group)
    manager = agent.job_manager
    if runtime is None or runtime.status != STATUS_LIVE:
        return None
    read_affected = runtime.descriptor.read_affected
    if read_affected is None:
        return None
    if cmd_def.read_only or cmd_def.self_reconciling:
        # Read-only mutates nothing; self-reconciling re-dispatches on a loop, so
        # no single success is the "did it land" moment a push would serve.
        return None

    # Each await can resume under a replaced or closing session, so every hop re-fences.
    async def refresh_and_push() -> None:
        if manager is None or manager.closing or agent.job_manager is not manager:
            return
        # The snapshot only plans WHICH items to re-read; the merge below reads
        # the CURRENT runtime.state, so a concurrent writer's change is never lost.
        state = runtime.state
        if state is None:
            return
        items = await asyncio.to_thread(read_affected, runtime.config, state, params)
        if manager.closing or agent.job_manager is not manager:
            return
        # Nothing read back (new-resource op, alias-only op, a delete's 404):
        # nothing to push; the periodic walk reflects it.
        if not items or not merge_items_into_runtime(runtime, items):
            return
        envelope = await build_metrics_envelope(agent)
        if (
            manager.closing
            or agent.job_manager is not manager
            or not await manager.send_now(envelope)
        ):
            discard_push(agent.integrations, envelope.id)
            return
        logger.info("Sent post-mutation metrics push for %s", command)

    return refresh_and_push


#: Stderr of a job refused because no live session can start it.
_SESSION_CLOSING = (
    "The command wasn't started because this connection is closing. "
    "Retry after the agent reconnects."
)


async def dispatch_long_running(
    agent: Agent,
    ws: ClientConnection,
    request_id: str,
    payload: CommandRequestPayload,
    cmd_def: CommandSpec,
) -> None:
    """Hand a long-running command off to the JobManager. Every refusal here is a
    terminal result (CORE-011 d7); a duplicate id is an ``error``, its first
    request still live, so no result may resolve it."""
    if agent.job_manager is None:
        logger.error("Cannot dispatch %r: no active JobManager", payload.command)
        failure = CommandResultPayload.failed(
            request_id,
            payload.command,
            cmd_def.group,
            FailureReason.SESSION_CLOSING,
            _SESSION_CLOSING,
        )
        await ws.send(make(agent.config.agent.id, failure).to_json())
        return

    # Same regex-param-validation as the subprocess path; closes the dispatch asymmetry.
    try:
        validated_params = validate_params(cmd_def, payload.params or {})
    except ParamValidationError as exc:
        logger.warning("Param validation failed for %s: %s", request_id, exc)
        failure = CommandResultPayload.failed(
            request_id,
            payload.command,
            cmd_def.group,
            FailureReason.VALIDATION_FAILED,
            str(exc),
        )
        await agent.job_manager.send_now(make(agent.config.agent.id, failure))
        return

    # The handler rides on the spec (single source); the thunk fires here with
    # validated params. It may still return None when the feature is registered
    # but unservable on this host.
    factory = cmd_def.handler
    handler = factory(validated_params) if factory is not None else None
    if handler is None:
        logger.error(
            "Command %r is marked long_running but no handler is registered",
            payload.command,
        )
        failure = CommandResultPayload.failed(
            request_id,
            payload.command,
            cmd_def.group,
            FailureReason.OS_ERROR,
            f"No long-running handler for {payload.command!r}",
        )
        await agent.job_manager.send_now(make(agent.config.agent.id, failure))
        return

    # Quota admission ceiling. A metrics-tick storm re-dispatches the same set_quota
    # mismatches every ~5s; past one execution wave the JobManager sheds them as
    # agent_overloaded rather than growing an unbounded accepted-but-queued
    # backlog. The manager owns the quota-command identity and the ceiling; this
    # path just asks. Runs no handler and mutates no state on rejection.
    if agent.job_manager.should_shed_quota(payload.command):
        logger.warning(
            "Shedding %r (request %s): quota admission ceiling reached",
            payload.command,
            request_id,
        )
        await agent.job_manager.reject_quota_overloaded(
            request_id,
            payload.command,
            cmd_def.group,
            params=non_secret_params(cmd_def, validated_params),
        )
        return

    on_success = post_success_hook(agent, cmd_def, payload.command, validated_params)
    # The handler (above) closed over the full params; the JobManager copy is only
    # event/log context, so secret-flagged params are dropped before it ever sees them.
    try:
        agent.job_manager.start(
            request_id,
            payload.command,
            cmd_def.group,
            handler,
            on_success=on_success,
            params=non_secret_params(cmd_def, validated_params),
            retry_window_s=cmd_def.retry_window_s,
        )
    except JobManagerClosing:
        # `start` is the one admission gate; a closed manager still sends.
        logger.info(
            "Refused %r (request %s): %s",
            payload.command,
            request_id,
            FailureReason.SESSION_CLOSING,
        )
        failure = CommandResultPayload.failed(
            request_id,
            payload.command,
            cmd_def.group,
            FailureReason.SESSION_CLOSING,
            _SESSION_CLOSING,
        )
        await agent.job_manager.send_now(make(agent.config.agent.id, failure))
    except ValueError:
        raise ProtocolError(
            f"Duplicate request {request_id}",
            reason="duplicate_request",
            msg_type=MessageType.COMMAND_REQUEST.value,
        ) from None


async def handle_command_sequence(
    agent: Agent,
    ws: ClientConnection,
    envelope: Envelope,
) -> None:
    """Verify and execute a command sequence, streaming results.

    Every result, a refusal included, carries the ``sequence_id`` and the
    ``step_index`` of the step it answers for (CORE-011 d9).
    """
    payload = _verify_typed_payload(agent, envelope, CommandSequencePayload)
    if payload is None:
        return
    if await refuse_incompatible_command(agent, ws, envelope, payload):
        return
    names = [step.command for step in payload.steps]
    logger.info("Executing sequence %s: %s", payload.sequence_id, names)

    def step_result(result: CommandResultPayload, index: int) -> str:
        stamped = replace(result, sequence_id=payload.sequence_id, step_index=index)
        return make(agent.config.agent.id, stamped).to_json()

    # Deflect a now-unsealed hatch command missing from the boot-built
    # registry before whitelist validation, which would otherwise reject it
    # as unknown and fire the security alarm (ADR CORE-004). Mirrors the
    # single-command path's _deflect_if_restart_pending.
    restart_cmd = needs_restart_to_load(agent.signoff_state, agent.registry, names)
    if restart_cmd is not None:
        logger.warning(
            "Deflected sequence %s: %s unsealed after boot, registry not "
            "rebuilt (restart required)",
            payload.sequence_id,
            restart_cmd,
        )
        result = restart_pending_result(str(uuid.uuid4()), restart_cmd)
        await ws.send(step_result(result, names.index(restart_cmd)))
        return

    for index, name in enumerate(names):
        try:
            get_command(name, registry=agent.registry)
        except CommandError as exc:
            logger.warning(
                "Sequence %s has invalid command: %s", payload.sequence_id, exc
            )
            # Authenticated request, so the refusal goes on the wire (one failure
            # result terminates the sequence dashboard-side), never silence.
            failure = CommandResultPayload.failed(
                str(uuid.uuid4()), name, _UNKNOWN_GROUP, exc.reason, str(exc)
            )
            await ws.send(step_result(failure, index))
            return

    # Sequence seal recheck mirrors the single-command path (ADR CORE-004).
    if is_blocked_by_seal(agent.signoff_state, names):
        sealed_in_sequence = sorted(SEALED_COMMANDS & set(names))
        logger.warning(
            "Refused sequence %s: contains %s while sealed",
            payload.sequence_id,
            ", ".join(sealed_in_sequence),
        )
        first_sealed = sealed_in_sequence[0]
        sealed = sealed_refusal_result(
            str(uuid.uuid4()), first_sealed, agent.registry.get(first_sealed)
        )
        await ws.send(step_result(sealed, names.index(first_sealed)))
        return

    project = agent.config.project
    for index, step in enumerate(payload.steps):
        request_id = str(uuid.uuid4())
        try:
            result = await asyncio.to_thread(
                execute_command,
                step.command,
                project,
                request_id,
                payload.sequence_id,
                registry=agent.registry,
                runtime_params=step.params or None,
            )
        except (CommandError, ParamValidationError) as exc:
            # A CommandError here is a grant revoked mid-sequence (CORE-007 D3).
            logger.warning("Sequence %s step %d: %s", payload.sequence_id, index, exc)
            result = CommandResultPayload.failed(
                request_id,
                step.command,
                agent.registry[step.command].group,
                exc.reason
                if isinstance(exc, CommandError)
                else FailureReason.VALIDATION_FAILED,
                str(exc),
            )
        await ws.send(step_result(result, index))
        logger.info(
            "Sequence %s step %d %r: success=%s, %dms",
            payload.sequence_id,
            index,
            step.command,
            result.success,
            result.duration_ms,
        )
        if payload.stop_on_failure and not result.success:
            logger.warning(
                "Sequence %s halted at step %d %r",
                payload.sequence_id,
                index,
                step.command,
            )
            break


# Every inbound or both-way type in the catalogue, one table; a test holds it exhaustive.
HANDLERS: Mapping[MessageType, Handler] = {
    **{t: handle_ack for t, spec in MESSAGES.items() if spec.payload is AckPayload},
    # The handshake consumed the real one before this loop started (CORE-011 d2);
    # a late duplicate is an ack.
    MessageType.REGISTER_OK: handle_ack,
    MessageType.METRICS_ACK: handle_metrics_ack,
    MessageType.ERROR: handle_error,
    MessageType.COMMAND_REQUEST: handle_command_request,
    MessageType.COMMAND_SEQUENCE: handle_command_sequence,
    MessageType.LOG_BATCH_ACK: handle_log_batch_ack,
    MessageType.EVENTS_BATCH_ACK: handle_events_batch_ack,
    MessageType.EVENTS_BATCH_NACK: handle_events_batch_nack,
}
