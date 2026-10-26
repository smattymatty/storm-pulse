"""Per-command admission against live agent state: seal, fence, restart-pending.

Both handlers call ``refusal_for`` before every execution, a sequence once per
step, so a mid-run reseal or revoke stops the next command (CORE-004 d6, CORE-007 D3).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from stormpulse import events
from stormpulse.agent.signoff_guard import (
    is_blocked_by_seal,
    needs_restart_to_load,
    restart_pending_result,
    sealed_refusal_result,
)
from stormpulse.protocol import CommandResultPayload, FailureReason

if TYPE_CHECKING:
    from stormpulse.agent import Agent

logger = logging.getLogger(__name__)
events.declare_event("commands", "whitelist_rejection", {"status", "command", "error"})
# A record, not an alarm: the operator sealed. No params, they carry shell text.
events.declare_event("signoff", "sealed_refusal", {"status", "command", "error"})


def refusal_for(
    agent: Agent, command: str, request_id: str, ref: str
) -> CommandResultPayload | None:
    """The refusal to send for *command*, or ``None`` to admit it.

    *request_id* rides the result; *ref* (the envelope id) names it in logs and
    events. Only the fence alarms: a seal or a pending restart is the operator's own.
    """
    return (
        _sealed(agent, command, request_id, ref)
        or _fenced(agent, command, request_id, ref)
        or _restart_pending(agent, command, request_id, ref)
    )


def emit_whitelist_rejection(command: str, ref: str, error: str) -> None:
    """The alarm for a command the whitelist refused: unknown, or fenced (D3)."""
    events.emit(
        "whitelist_rejection",
        source="commands",
        status="refused",
        command=str(command)[:100],
        command_ref=str(ref)[:100],
        error=error,
    )


def sealed_refusal(
    request_id: str, command: str, agent: Agent, ref: str
) -> CommandResultPayload:
    """The ``signoff_sealed`` result for *command*, recorded as one ``sealed_refusal``."""
    result = sealed_refusal_result(request_id, command, agent.registry.get(command))
    events.emit(
        "sealed_refusal",
        source="signoff",
        status="refused",
        command=str(command)[:100],
        command_ref=str(ref)[:100],
        error=result.error.message if result.error else "",
    )
    return result


def _sealed(
    agent: Agent, command: str, request_id: str, ref: str
) -> CommandResultPayload | None:
    """A sealed verify/apply block (ADR CORE-004)."""
    if not is_blocked_by_seal(agent.signoff_state, [command]):
        return None
    logger.warning("Refused %s (request %s): signoff is sealed", command, ref)
    return sealed_refusal(request_id, command, agent, ref)


def _fenced(
    agent: Agent, command: str, request_id: str, ref: str
) -> CommandResultPayload | None:
    """A command its current grant no longer authorizes, in every mode (CORE-007 D3)."""
    cmd_def = agent.registry.get(command)
    if cmd_def is None or cmd_def.authorize is None:
        return None
    refused = cmd_def.authorize()
    if refused is None:
        return None
    message = f"Command {command!r} is fenced: {refused}"
    logger.warning("Refused %s (request %s): %s", command, ref, refused)
    emit_whitelist_rejection(command, ref, message)
    return CommandResultPayload.failed(
        request_id, command, cmd_def.group, FailureReason.FORBIDDEN, message
    )


def _restart_pending(
    agent: Agent, command: str, request_id: str, ref: str
) -> CommandResultPayload | None:
    """A now-unsealed hatch the boot-built registry has not loaded yet.

    Asks for a restart instead of falling through to the whitelist path, which
    would alarm on the operator's own legitimate command (ADR CORE-004).
    """
    restart_cmd = needs_restart_to_load(agent.signoff_state, agent.registry, [command])
    if restart_cmd is None:
        return None
    logger.warning(
        "Deflected %s (request %s): unsealed after boot, registry not rebuilt "
        "(restart required)",
        command,
        ref,
    )
    return restart_pending_result(request_id, restart_cmd)
