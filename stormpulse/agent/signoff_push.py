"""Push `signoff.state` envelopes when the on-host seal sentinel flips.

Sibling of ``signoff_nag``. The nag loop warns periodically while the
agent is unsealed; this loop watches for the transition itself and
pushes a single ``signoff.state`` envelope so the dashboard's mirror
updates without waiting for a reconnect-driven register. See ADR
CORE-004 "Live propagation" and the `signoff.state` section of the
Pulse wire contract spec.

Single responsibility: detect a transition on the sentinel file and
push the envelope. The poll cadence is short (5s) because seal/unseal
is an operator-driven action that wants prompt dashboard feedback;
the cost is one ``stat`` per tick which is negligible. The initial
state at connection time is already advertised by ``register``, so
this loop seeds ``last_sealed`` from the agent's current state and
emits only on subsequent transitions.

Missed transitions across a dropped WebSocket self-heal: the next
``register`` after reconnect carries the post-transition snapshot.
There is no replay buffer here on purpose.
"""

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
    agent_id = agent._config.agent.id
    state = agent._signoff_state
    last_sealed = state.is_sealed()
    while not agent._shutdown.is_set():
        if await sleep_or_shutdown(agent._shutdown, POLL_INTERVAL_SECONDS):
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
        if agent._pulse_logger is not None:
            agent._pulse_logger.info(
                "Seal state changed",
                "signoff",
                {"sealed": current_sealed},
            )
