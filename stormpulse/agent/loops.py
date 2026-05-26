"""The four periodic tasks the Agent runs concurrently per connection.

Each loop is a free async function so it can be unit-tested with a
fake ``Agent`` context rather than a full Agent instance. The loops
read state from the agent (config, garage_state, shippers,
pending_batches) and write back where applicable; they do not own the
websocket lifecycle (the outer ``Agent.run`` does).

The shutdown event is checked at every iteration boundary via
``sleep_or_shutdown`` so loops exit promptly when the agent is torn
down.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import TYPE_CHECKING

from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from stormpulse.garage.state import collect_garage_state
from stormpulse.metrics import collect_metrics
from stormpulse.protocol import (
    make_heartbeat,
    make_log_batch,
    make_metrics_push,
)

if TYPE_CHECKING:
    from stormpulse.agent import Agent

logger = logging.getLogger(__name__)


async def sleep_or_shutdown(
    shutdown: asyncio.Event, interval: float,
) -> bool:
    """Sleep *interval* seconds or return early when ``shutdown`` fires.

    Returns ``True`` when shutdown triggered, ``False`` on timeout. Shared by
    every periodic loop so each one doesn't re-implement the ``wait_for`` dance.
    """
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=interval)
        return True
    except TimeoutError:
        return False


async def heartbeat_loop(agent: "Agent", ws: ClientConnection) -> None:
    """Send periodic heartbeats until shutdown or disconnect."""
    interval = agent._config.dashboard.heartbeat_interval_seconds
    agent_id = agent._config.agent.id
    while not agent._shutdown.is_set():
        heartbeat = make_heartbeat(agent_id)
        await ws.send(heartbeat.to_json())
        logger.debug("Sent heartbeat %s", heartbeat.id)
        if await sleep_or_shutdown(agent._shutdown, interval):
            return


async def metrics_loop(agent: "Agent", ws: ClientConnection) -> None:
    """Collect and push metrics at the configured interval."""
    interval = agent._config.metrics.push_interval_seconds
    agent_id = agent._config.agent.id
    while not agent._shutdown.is_set():
        try:
            metrics = await asyncio.to_thread(collect_metrics, agent._config)
            garage_dict = agent._garage_state.to_dict() if agent._garage_state else None
            envelope = make_metrics_push(agent_id, metrics, garage=garage_dict)
            await ws.send(envelope.to_json())
            logger.debug("Sent metrics push %s", envelope.id)
        except ConnectionClosed:
            raise
        except Exception:
            logger.warning("Failed to collect/send metrics", exc_info=True)
        if await sleep_or_shutdown(agent._shutdown, interval):
            return


async def garage_loop(agent: "Agent", ws: ClientConnection) -> None:
    """Refresh Garage state at the configured interval.

    No-op when ``config.garage`` is None or disabled. Writes
    ``agent._garage_state``; the metrics loop reads it each cycle to
    bundle the latest snapshot into the next push.
    """
    gc = agent._config.garage
    if gc is None or not gc.enabled:
        return
    interval = gc.state_push_interval_seconds
    while not agent._shutdown.is_set():
        try:
            state = await asyncio.to_thread(collect_garage_state, gc)
            if state is not None:
                agent._garage_state = state
                logger.debug("Refreshed garage state")
        except Exception:
            logger.warning("Failed to collect garage state", exc_info=True)
        if await sleep_or_shutdown(agent._shutdown, interval):
            return


async def log_loop(agent: "Agent", ws: ClientConnection, group_name: str) -> None:
    """Tail, parse, batch, and ship logs for one group."""
    shipper = agent._shippers[group_name]
    interval = shipper.ship_interval_seconds
    agent_id = agent._config.agent.id
    while not agent._shutdown.is_set():
        try:
            agent._pending_batches.prune_stale()

            batch = await asyncio.to_thread(shipper.collect_batch)
            if batch is not None:
                batch_id = str(uuid.uuid4())
                envelope = make_log_batch(
                    agent_id,
                    group=group_name,
                    parser=shipper.parser_name,
                    batch_id=batch_id,
                    lines=batch.lines,
                    dropped=batch.dropped,
                    from_position=batch.from_position,
                    to_position=batch.to_position,
                )
                agent._pending_batches.add(batch_id, group_name, batch.to_position)
                await ws.send(envelope.to_json())
                logger.debug(
                    "Sent log.batch %s group=%s lines=%d dropped=%d",
                    batch_id, group_name, len(batch.lines), batch.dropped,
                )
        except ConnectionClosed:
            raise
        except Exception:
            logger.warning("Log loop error for group %s", group_name, exc_info=True)

        if await sleep_or_shutdown(agent._shutdown, interval):
            return
