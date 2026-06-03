"""Push ``signoff.state`` on observed sentinel transitions (ADR CORE-004 live propagation)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from stormpulse.agent.loops import sleep_or_shutdown
from stormpulse.protocol import make_signoff_state

if TYPE_CHECKING:
    from stormpulse.agent import Agent

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5.0


async def signoff_state_push_loop(
    agent: Agent,
    ws: ClientConnection,
) -> None:
    """Push ``signoff.state`` on every observed seal transition."""
    agent_id = agent.config.agent.id
    state = agent.signoff_state
    last_sealed = state.is_sealed()
    while not agent.shutdown.is_set():
        if await sleep_or_shutdown(agent.shutdown, POLL_INTERVAL_SECONDS):
            return
        current_sealed = state.is_sealed()
        if current_sealed == last_sealed:
            continue
        since = state.unsealed_since()
        envelope = make_signoff_state(
            agent_id,
            sealed=current_sealed,
            unsealed_since=since.isoformat() if since else None,
        )
        try:
            await ws.send(envelope.to_json())
        except ConnectionClosed:
            raise
        except Exception:
            logger.warning(
                "Failed to push signoff.state on transition",
                exc_info=True,
            )
            # Retry next tick; do not advance last_sealed on send failure.
            continue
        logger.info(
            "Pushed signoff.state (sealed=%s) on observed transition",
            current_sealed,
        )
        last_sealed = current_sealed
        if agent.pulse_logger is not None:
            agent.pulse_logger.info(
                "Seal state changed",
                "signoff",
                {"sealed": current_sealed},
            )
