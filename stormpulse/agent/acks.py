"""Inbound frames that execute nothing: acks release what they confirm, an error is logged."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from websockets.asyncio.client import ClientConnection

from stormpulse import events
from stormpulse.agent.integrations_runtime import acknowledge_push
from stormpulse.protocol import Envelope, MetricsAckPayload

if TYPE_CHECKING:
    from stormpulse.agent import Agent

logger = logging.getLogger(__name__)

_BATCH_ID = "batch_id"


async def handle_events_batch_ack(
    _agent: Agent, _ws: ClientConnection, envelope: Envelope
) -> None:
    """Release an acknowledged events batch from the in-process buffer."""
    batch_id = envelope.payload.get(_BATCH_ID)
    if not isinstance(batch_id, str):
        logger.warning("events.batch.ack missing batch_id")
        return
    if not events.buffer().ack(batch_id):
        logger.debug("events.batch.ack for unknown batch_id %s", batch_id)


async def handle_events_batch_nack(
    _agent: Agent, _ws: ClientConnection, envelope: Envelope
) -> None:
    """Drop a refused events batch and say so once, with the control plane's reason (CORE-011 d6)."""
    batch_id = envelope.payload.get(_BATCH_ID)
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
    """Confirm an acknowledged batch with its shipper, then resolve it."""
    batch_id = envelope.payload.get(_BATCH_ID)
    if not isinstance(batch_id, str):
        logger.warning("log.batch.ack missing batch_id")
        return
    receipt = agent.pending_batches.peek(batch_id)
    if receipt is None:
        logger.debug("log.batch.ack for unknown batch_id %s", batch_id)
        return
    # Confirm, then pop: a read between the two would see the range lost and rewind.
    shipper = agent.shippers.get(receipt.group)
    try:
        confirmed = shipper is not None and await asyncio.to_thread(
            shipper.confirm, receipt
        )
    finally:
        agent.pending_batches.pop(batch_id)
    if confirmed:
        logger.debug(
            "Confirmed batch %s for group %s (range ends %s)",
            batch_id,
            receipt.group,
            receipt.to_position,
        )


async def handle_metrics_ack(
    agent: Agent, _ws: ClientConnection, envelope: Envelope
) -> None:
    """Release reads covered by the explicitly acknowledged metrics push."""
    ack = MetricsAckPayload.from_dict(envelope.payload)
    if not isinstance(ack.push_id, str) or not ack.push_id:
        logger.warning("metrics.ack requires a non-empty push_id")
        return
    if not acknowledge_push(agent.integrations, ack.push_id):
        logger.debug("metrics.ack for unknown push_id %s", ack.push_id)


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
