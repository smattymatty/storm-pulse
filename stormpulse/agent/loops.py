"""Periodic per-connection task bodies (heartbeat, metrics, integration state, log shipping) plus shared ``sleep_or_shutdown``."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from stormpulse import events
from stormpulse.agent.integrations_runtime import (
    build_metrics_envelope,
    merge_items_into_runtime,
)
from stormpulse.integrations import LogEnricher
from stormpulse.protocol import make_events_batch, make_heartbeat, make_log_batch

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
    while not agent.shutdown.is_set():
        try:
            envelope = await build_metrics_envelope(agent)
            await ws.send(envelope.to_json())
            logger.debug("Sent metrics push %s", envelope.id)
        except ConnectionClosed:
            raise
        except Exception:
            logger.warning("Failed to collect/send metrics", exc_info=True)
        if await sleep_or_shutdown(agent.shutdown, interval):
            return


async def integration_state_loop(
    agent: Agent, ws: ClientConnection, integ_id: str
) -> None:
    """Refresh one live Integration's state on the metrics-push cadence (CORE-005
    decision 9: faster collection is discarded work that still pays backend cost)."""
    runtime = agent.integrations[integ_id]
    # collect_state is the Integration's own reader; garage composes its
    # sub-cadences inside GarageStateReader (decision 9), never a knob here.
    descriptor = runtime.descriptor
    assert descriptor.collect_state is not None
    interval = agent.config.metrics.push_interval_seconds
    # Attribute every admin call made under this loop (contextvars
    # propagate into asyncio.to_thread) to the periodic walk.
    events.trigger_var.set("periodic_walk")
    while not agent.shutdown.is_set():
        try:
            state = await asyncio.to_thread(descriptor.collect_state, runtime.config)
            if state is not None:
                runtime.state = state
                logger.debug("Refreshed %s state", integ_id)
        except Exception:
            logger.warning("Failed to collect %s state", integ_id, exc_info=True)
        if await sleep_or_shutdown(agent.shutdown, interval):
            return


async def integration_detect_loop(
    agent: Agent, ws: ClientConnection, integ_id: str
) -> None:
    """Run one Integration's new-resource detector at its own interval - the one
    tunable state-read cadence, a security dial (CORE-005 decision 9)."""
    runtime = agent.integrations[integ_id]
    detector = runtime.descriptor.detect
    assert detector is not None
    interval = detector.interval(runtime.config)
    # Attribute admin calls made under this loop to the detector.
    events.trigger_var.set("detector")
    while not agent.shutdown.is_set():
        try:
            # The snapshot handed to detect is only the diff baseline; the merge
            # reads the CURRENT state (decision 11 race discipline), then push.
            newcomers = await asyncio.to_thread(detector.run, runtime.config, runtime.state)
            if newcomers and merge_items_into_runtime(runtime, newcomers):
                envelope = await build_metrics_envelope(agent)
                await ws.send(envelope.to_json())
                logger.info(
                    "Detector pushed %d new %s resource(s)",
                    len(newcomers),
                    integ_id,
                )
        except ConnectionClosed:
            raise
        except Exception:
            logger.warning("Detect loop error for %s", integ_id, exc_info=True)
        if await sleep_or_shutdown(agent.shutdown, interval):
            return


async def log_loop(
    agent: Agent,
    ws: ClientConnection,
    group_name: str,
    resolver_provider: Callable[[], LogEnricher | None],
) -> None:
    """Tail, parse, batch, and ship logs for one group. ``resolver_provider`` is its
    tick-fresh enrichment join, wired from ``log_enrichers`` by the composition root."""
    shipper = agent.shippers[group_name]
    interval = shipper.ship_interval_seconds
    agent_id = agent.config.agent.id
    while not agent.shutdown.is_set():
        try:
            agent.pending_batches.prune_stale()

            started = time.monotonic()
            # Tick-fresh enrichment map; state is reassigned
            # atomically, so each tick sees a consistent view.
            resolver = resolver_provider()
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
                # INFO + duration_ms while shipping is being proven out; drop to
                # debug once steady.
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


async def events_loop(agent: Agent, ws: ClientConnection) -> None:
    """Drain the wide-event buffer and ship ``events.batch`` envelopes.

    Pinned to the metrics push cadence: events power read-time analysis,
    not liveness, so a dedicated interval knob would be a constant
    wearing a dial's clothing (the capacity-model rule). Batches stay
    in-flight in the buffer until the dashboard acks them; the fresh
    session requeues whatever the previous connection never acked
    (see reconnect).
    """
    interval = agent.config.metrics.push_interval_seconds
    buf = events.buffer()
    agent_id = agent.config.agent.id
    while not agent.shutdown.is_set():
        try:
            batch_id = str(uuid.uuid4())
            batch = buf.drain(batch_id)
            if batch:
                envelope = make_events_batch(
                    agent_id, batch_id=batch_id, events=batch
                )
                await ws.send(envelope.to_json())
                logger.debug(
                    "Shipped events.batch %s events=%d", batch_id, len(batch)
                )
        except ConnectionClosed:
            raise
        except Exception:
            logger.warning("Events loop error", exc_info=True)
        if await sleep_or_shutdown(agent.shutdown, interval):
            return
