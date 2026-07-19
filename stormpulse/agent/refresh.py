"""Generic on-demand refresh: the ``{id}_refresh`` command (synthesized for any
Integration declaring ``collect_state``) collects now and pushes fresh metrics."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from websockets.asyncio.client import ClientConnection

from stormpulse.agent.integrations_runtime import build_metrics_envelope
from stormpulse.protocol import CommandResultPayload, make_command_result

if TYPE_CHECKING:
    from stormpulse.agent import Agent

logger = logging.getLogger(__name__)


async def collect_refresh_result(
    agent: Agent,
    command: str,
    request_id: str,
    integ_id: str,
) -> CommandResultPayload:
    """Collect a fresh snapshot for the command's integration and store it; no wire IO.
    ``integ_id`` is the spec's group (== id, boot-enforced), resolved once at dispatch."""
    runtime = agent.integrations.get(integ_id)
    if runtime is None or runtime.status != "live" or runtime.descriptor.collect_state is None:
        return CommandResultPayload(
            request_id=request_id,
            command=command,
            group=integ_id,
            success=False,
            exit_code=-1,
            stdout="",
            stderr=f"Integration {integ_id!r} not enabled",
            duration_ms=0,
            failure_reason="not_configured",
        )
    start = time.monotonic()
    # An explicit refresh must not serve cached sub-state (garage's
    # topology cache made a fresh layout change invisible here); prefer
    # the cache-bypassing collector when the Integration declares one.
    collector = runtime.descriptor.collect_state_fresh or runtime.descriptor.collect_state
    state = await asyncio.to_thread(collector, runtime.config)
    duration_ms = int((time.monotonic() - start) * 1000)
    if state is None:
        return CommandResultPayload(
            request_id=request_id,
            command=command,
            group=integ_id,
            success=False,
            exit_code=-1,
            stdout="",
            stderr=f"Failed to collect {integ_id} state",
            duration_ms=duration_ms,
            failure_reason="collection_failed",
        )
    runtime.state = state
    # Optional per-state summary (default when a state type doesn't define one).
    # Preserves the pre-single-source "Refreshed: N buckets" line for garage.
    summary = state.summary() if hasattr(state, "summary") else None
    stdout = f"Refreshed: {summary}" if summary else f"Refreshed {integ_id} state"
    return CommandResultPayload(
        request_id=request_id,
        command=command,
        group=integ_id,
        success=True,
        exit_code=0,
        stdout=stdout,
        stderr="",
        duration_ms=duration_ms,
    )


async def handle_refresh(
    agent: Agent,
    ws: ClientConnection,
    command: str,
    request_id: str,
    integ_id: str,
) -> None:
    """Inline refresh ceremony: collect, send result, pulse-log, push metrics on success."""
    result = await collect_refresh_result(agent, command, request_id, integ_id)
    await ws.send(make_command_result(agent.config.agent.id, result).to_json())
    logger.info(
        "Sent result for %r: success=%s, %dms",
        command,
        result.success,
        result.duration_ms,
    )
    if agent.pulse_logger is not None:
        cmd_def = agent.registry.get(command)
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
        logger.info("Sent immediate metrics push after %s", command)
    except Exception:
        logger.warning("Failed to send metrics after %s", command, exc_info=True)
