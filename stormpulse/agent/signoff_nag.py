"""Periodic warning loop while the agent is unsealed (ADR CORE-004). Cheap; no-op when sealed."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from websockets.asyncio.client import ClientConnection

from stormpulse.agent.loops import sleep_or_shutdown
from stormpulse.signoff import format_unsealed_duration

if TYPE_CHECKING:
    from stormpulse.agent import Agent

logger = logging.getLogger(__name__)

NAG_INTERVAL_SECONDS = 300.0  # 5 minutes


async def signoff_nag_loop(agent: Agent, ws: ClientConnection) -> None:
    """Warn periodically while the agent is unsealed.

    ``ws`` is unused (the dashboard learns seal state via register); the
    parameter is here to match the other loop bodies' signature so the
    task group can launch this the same way as the others.
    """
    del ws  # interface symmetry with other loops
    while not agent.shutdown.is_set():
        if not agent.signoff_state.is_sealed():
            duration = format_unsealed_duration(
                agent.signoff_state.unsealed_since(),
            )
            logger.warning(
                "Agent is UNSEALED (for %s). The dashboard verify-block "
                "hatch is open. Reseal with `stormpulse signoff seal` as "
                "soon as verification is complete.",
                duration,
            )
            if agent.pulse_logger is not None:
                agent.pulse_logger.warning(
                    "Agent unsealed",
                    "signoff",
                    {"duration": duration},
                )
        if await sleep_or_shutdown(agent.shutdown, NAG_INTERVAL_SECONDS):
            return
