"""Garage-state side effects of command dispatch.

``garage_refresh`` is the one internal command the dispatcher handles
inline (no subprocess, no JobManager). When it succeeds — or when any
``garage``-group long-running command finishes successfully — the
agent pushes a fresh ``metrics.push`` so the dashboard sees the
post-mutation snapshot in the same tick, not after the next scheduled
``state_push_interval_seconds`` window.

These helpers own that side-effect cluster: state refresh, envelope
build, and the two emission paths (sync via the websocket, async via
the JobManager's send_now).
"""

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
    make_metrics_push,
)

if TYPE_CHECKING:
    from stormpulse.agent import Agent

logger = logging.getLogger(__name__)


async def handle_garage_refresh(
    agent: Agent,
    request_id: str,
) -> CommandResultPayload:
    """Collect fresh Garage state and update shared state.

    Returns a ``CommandResultPayload``. The caller emits a
    ``metrics.push`` carrying the updated state separately.
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


async def push_post_refresh_metrics(
    agent: Agent,
    ws: ClientConnection,
) -> None:
    """Push a fresh metrics envelope right after a successful sync refresh.

    Used inline by ``handle_command_request`` for the ``garage_refresh``
    command. Swallows errors so a metrics-push failure doesn't mask the
    successful refresh.
    """
    try:
        envelope_out = await build_metrics_envelope(agent)
        await ws.send(envelope_out.to_json())
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

    Any successful long-running command in the ``garage`` group
    triggers an immediate Garage state refresh + ``metrics.push``.
    Otherwise the next scheduled metrics push (up to
    ``state_push_interval_seconds`` later) would overwrite the
    dashboard's just-updated bucket counts with the pre-mutation
    snapshot.
    """
    if cmd_def.group != "garage":
        return None
    gc = agent.config.garage
    if gc is None or not gc.enabled:
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
    if gc is None:
        return
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
