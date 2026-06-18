"""Periodic per-connection task bodies (heartbeat, metrics, garage, log shipping) plus shared ``sleep_or_shutdown``."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import TYPE_CHECKING

from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from stormpulse.garage.bucket_resolver import BucketIdResolver
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
    shutdown: asyncio.Event,
    interval: float,
) -> bool:
    """Sleep *interval* seconds; returns ``True`` if shutdown fired, ``False`` on timeout."""
    try:
        await asyncio.wait_for(shutdown.wait(), timeout=interval)
        return True
    except TimeoutError:
        return False


async def heartbeat_loop(agent: Agent, ws: ClientConnection) -> None:
    """Send periodic heartbeats until shutdown or disconnect."""
    interval = agent.config.dashboard.heartbeat_interval_seconds
    agent_id = agent.config.agent.id
    while not agent.shutdown.is_set():
        heartbeat = make_heartbeat(agent_id)
        await ws.send(heartbeat.to_json())
        logger.debug("Sent heartbeat %s", heartbeat.id)
        if await sleep_or_shutdown(agent.shutdown, interval):
            return


async def metrics_loop(agent: Agent, ws: ClientConnection) -> None:
    """Collect and push metrics at the configured interval."""
    interval = agent.config.metrics.push_interval_seconds
    agent_id = agent.config.agent.id
    while not agent.shutdown.is_set():
        try:
            metrics = await asyncio.to_thread(collect_metrics, agent.config)
            garage_dict = agent.garage_state.to_dict() if agent.garage_state else None
            envelope = make_metrics_push(agent_id, metrics, garage=garage_dict)
            await ws.send(envelope.to_json())
            logger.debug("Sent metrics push %s", envelope.id)
        except ConnectionClosed:
            raise
        except Exception:
            logger.warning("Failed to collect/send metrics", exc_info=True)
        if await sleep_or_shutdown(agent.shutdown, interval):
            return


async def garage_loop(agent: Agent, ws: ClientConnection) -> None:
    """Refresh Garage state at the configured interval. Gated by ``garage_live``."""
    gc = agent.config.garage
    assert gc is not None
    interval = gc.state_push_interval_seconds
    while not agent.shutdown.is_set():
        try:
            state = await asyncio.to_thread(collect_garage_state, gc)
            if state is not None:
                agent.garage_state = state
                logger.debug("Refreshed garage state")
        except Exception:
            logger.warning("Failed to collect garage state", exc_info=True)
        if await sleep_or_shutdown(agent.shutdown, interval):
            return


async def log_loop(agent: Agent, ws: ClientConnection, group_name: str) -> None:
    """Tail, parse, batch, and ship logs for one group."""
    shipper = agent.shippers[group_name]
    interval = shipper.ship_interval_seconds
    agent_id = agent.config.agent.id
    while not agent.shutdown.is_set():
        try:
            agent.pending_batches.prune_stale()

            started = time.monotonic()
            # Build the (key_id, name) -> bucket_id map from the latest
            # garage-state snapshot the garage_loop published (BUCKETS-015).
            # garage_state is reassigned atomically, so the captured snapshot
            # is a consistent, immutable view for this tick.
            resolver = BucketIdResolver.from_state(agent.garage_state)
            batch = await asyncio.to_thread(shipper.collect_batch, resolver)
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
                agent.pending_batches.add(batch_id, group_name, batch.to_position)
                await ws.send(envelope.to_json())
                # INFO + duration_ms while bringing log shipping up, same shape as
                # the garage job logs, so the collect+ship cost is visible at a 2s
                # interval. Drop to logger.debug once shipping is proven steady.
                duration_ms = int((time.monotonic() - started) * 1000)
                logger.info(
                    "Shipped log.batch %s group=%s lines=%d dropped=%d duration_ms=%d",
                    batch_id,
                    group_name,
                    len(batch.lines),
                    batch.dropped,
                    duration_ms,
                )
        except ConnectionClosed:
            raise
        except Exception:
            logger.warning("Log loop error for group %s", group_name, exc_info=True)

        if await sleep_or_shutdown(agent.shutdown, interval):
            return
