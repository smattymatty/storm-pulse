"""Garage-state side effects: ``handle_garage_refresh`` (inline ceremony) and ``post_success_hook`` (JobManager after-success)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from websockets.asyncio.client import ClientConnection

from stormpulse.config import CommandDef
from stormpulse.garage.state import collect_garage_state
from stormpulse.metrics import collect_metrics
from stormpulse.protocol import (
    CommandResultPayload,
    Envelope,
    make_command_result,
    make_metrics_push,
)

if TYPE_CHECKING:
    from stormpulse.agent import Agent

logger = logging.getLogger(__name__)


async def collect_refresh_result(
    agent: Agent,
    request_id: str,
) -> CommandResultPayload:
    """Collect a fresh Garage snapshot and build the resulting payload.

    Updates ``agent.garage_state`` on success. Returns a structured
    failure payload when garage is disabled or collection failed. No
    wire IO — the caller is responsible for sending and for the
    post-success metrics push.
    """
    gc = agent.config.garage
    if gc is None or not gc.enabled:
        return CommandResultPayload(
            request_id=request_id,
            command="garage_refresh",
            group="garage",
            success=False,
            exit_code=-1,
            stdout="",
            stderr="Garage integration not enabled",
            duration_ms=0,
            failure_reason="not_configured",
        )
    start = time.monotonic()
    state = await asyncio.to_thread(collect_garage_state, gc)
    duration_ms = int((time.monotonic() - start) * 1000)
    if state is not None:
        agent.garage_state = state
        return CommandResultPayload(
            request_id=request_id,
            command="garage_refresh",
            group="garage",
            success=True,
            exit_code=0,
            stdout=f"Refreshed: {len(state.buckets)} buckets",
            stderr="",
            duration_ms=duration_ms,
        )
    return CommandResultPayload(
        request_id=request_id,
        command="garage_refresh",
        group="garage",
        success=False,
        exit_code=-1,
        stdout="",
        stderr="Failed to collect garage state",
        duration_ms=duration_ms,
        failure_reason="collection_failed",
    )


async def handle_garage_refresh(
    agent: Agent,
    ws: ClientConnection,
    request_id: str,
) -> None:
    """Run the inline ``garage_refresh`` dispatch ceremony.

    Symmetric with ``dispatch_long_running`` for the long-running path:
    this function owns the full per-command IO (collect, send the
    command.result, pulse-log, push fresh metrics on success). The
    dispatcher early-returns after calling this and never touches the
    result.

    The immediate metrics push lets the dashboard see the post-refresh
    snapshot in the same tick as the result rather than waiting up to
    ``state_push_interval_seconds`` for the next scheduled push. A
    metrics-push failure here is swallowed so it doesn't mask the
    successful refresh.
    """
    result = await collect_refresh_result(agent, request_id)
    await ws.send(make_command_result(agent.config.agent.id, result).to_json())
    logger.info(
        "Sent result for 'garage_refresh': success=%s, %dms",
        result.success,
        result.duration_ms,
    )
    if agent.pulse_logger is not None:
        cmd_def = agent.registry.get("garage_refresh")
        sensitive = cmd_def.sensitive_output if cmd_def else False
        agent.pulse_logger.log_command_result(
            command=result.command,
            success=result.success,
            duration_ms=result.duration_ms,
            sensitive=sensitive,
        )
    if not result.success:
        return
    try:
        envelope = await build_metrics_envelope(agent)
        await ws.send(envelope.to_json())
        logger.info("Sent immediate metrics push after garage_refresh")
    except Exception:
        logger.warning(
            "Failed to send metrics after garage_refresh",
            exc_info=True,
        )


def post_success_hook(
    agent: Agent,
    cmd_def: CommandDef,
    command: str,
) -> Callable[[], Awaitable[None]] | None:
    """Build the after-success callback for a long-running command, or ``None``.

    Garage long-runners push fresh state immediately so the next scheduled
    metrics window doesn't overwrite the dashboard with the pre-mutation snapshot.
    """
    if cmd_def.group != "garage":
        return None

    async def refresh_and_push() -> None:
        if agent.job_manager is None:
            return
        await refresh_garage_state(agent)
        envelope = await build_metrics_envelope(agent)
        await agent.job_manager.send_now(envelope)
        logger.info("Sent post-mutation metrics push for %s", command)

    return refresh_and_push


async def refresh_garage_state(agent: Agent) -> None:
    """Collect a fresh Garage snapshot and store it on the agent."""
    gc = agent.config.garage
    assert gc is not None
    state = await asyncio.to_thread(collect_garage_state, gc)
    if state is not None:
        agent.garage_state = state


async def build_metrics_envelope(agent: Agent) -> Envelope:
    """Bundle host metrics + the latest Garage snapshot into a ``metrics.push``."""
    metrics = await asyncio.to_thread(collect_metrics, agent.config)
    garage_dict = agent.garage_state.to_dict() if agent.garage_state else None
    return make_metrics_push(
        agent.config.agent.id,
        metrics,
        garage=garage_dict,
    )
