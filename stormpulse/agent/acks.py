"""Inbound frames that execute nothing: acks release what they confirm, an error is logged."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from websockets.asyncio.client import ClientConnection

from stormpulse import events
from stormpulse.agent.integrations_runtime import acknowledge_push
from stormpulse.protocol import Envelope

if TYPE_CHECKING:
    from stormpulse.agent import Agent

logger = logging.getLogger(__name__)


async def handle_events_batch_ack(
    _agent: Agent, _ws: ClientConnection, envelope: Envelope
) -> None:
    """Release an acknowledged events batch from the in-process buffer."""
    batch_id = envelope.payload.get("batch_id")
    if not isinstance(batch_id, str):
        logger.warning("events.batch.ack missing batch_id")
        return
    if not events.buffer().ack(batch_id):
        logger.debug("events.batch.ack for unknown batch_id %s", batch_id)


async def handle_events_batch_nack(
    _agent: Agent, _ws: ClientConnection, envelope: Envelope
) -> None:
    """Drop a refused events batch and say so once, with the control plane's reason (CORE-011 d6)."""
    batch_id = envelope.payload.get("batch_id")
    if not isinstance(batch_id, str):
        logger.warning("events.batch.nack missing batch_id")
        return
    dropped = events.buffer().nack(batch_id)
    if dropped is None:
        logger.debug("events.batch.nack for unknown batch_id %s", batch_id)
        return
    reason = str(envelope.payload.get("reason") or "")
    logger.warning("events batch %s refused (%d events): %s", batch_id, dropped, reason)
    events.emit(
        "events_nacked",
        source="events",
        batch_id=batch_id,
        dropped=dropped,
        reason=reason,
    )


async def handle_log_batch_ack(
    agent: Agent, _ws: ClientConnection, envelope: Envelope
) -> None:
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


async def handle_metrics_ack(
    agent: Agent, _ws: ClientConnection, envelope: Envelope
) -> None:
    """A ``metrics.ack``: the ids the last push carried are no longer owed (CORE-011 d5)."""
    acknowledge_push(agent.integrations)
    logger.debug("Received metrics.ack (%s)", envelope.id)


async def handle_ack(_agent: Agent, _ws: ClientConnection, envelope: Envelope) -> None:
    """A dashboard ack: received, logged at debug, never acted on."""
    logger.debug("Received %s (%s)", envelope.type.value, envelope.id)


async def handle_error(
    _agent: Agent, _ws: ClientConnection, envelope: Envelope
) -> None:
    """A dashboard error: the agent cannot correct it, so it says what was refused. Never an ack."""
    logger.warning(
        "Dashboard error (%s): %s, type %s",
        envelope.id,
        envelope.payload.get("error"),
        envelope.payload.get("type"),
    )
