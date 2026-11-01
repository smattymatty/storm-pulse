"""Periodic per-connection task bodies (heartbeat, metrics, integration state, log shipping, event spools) plus shared ``sleep_or_shutdown``."""

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
from stormpulse.agent.event_spool import SpoolFeeder
from stormpulse.agent.integrations_runtime import (
    build_metrics_envelope,
    discard_push,
    replace_runtime_state,
)
from stormpulse.integrations import LogEnricher
from stormpulse.logging.shipper import LogShipper
from stormpulse.protocol import (
    EventsBatchPayload,
    HeartbeatPayload,
    LogBatchPayload,
    make,
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
        heartbeat = make(agent_id, HeartbeatPayload())
        await ws.send(heartbeat.to_json())
        logger.debug("Sent heartbeat %s", heartbeat.id)
        if await sleep_or_shutdown(agent.shutdown, interval):
            return


async def metrics_loop(agent: Agent, ws: ClientConnection) -> None:
    """Collect and push metrics at the configured interval."""
    interval = agent.config.metrics.push_interval_seconds
    while not agent.shutdown.is_set():
        envelope = None
        try:
            envelope = await build_metrics_envelope(agent)
            await ws.send(envelope.to_json())
            logger.debug("Sent metrics push %s", envelope.id)
        except ConnectionClosed:
            if envelope is not None:
                discard_push(agent.integrations, envelope.id)
            raise
        except Exception:
            if envelope is not None:
                discard_push(agent.integrations, envelope.id)
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
                replace_runtime_state(runtime, state)
                logger.debug("Refreshed %s state", integ_id)
        except Exception:
            logger.warning("Failed to collect %s state", integ_id, exc_info=True)
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
    # A pull read starts at the Checkpoint, which only an ACK moves, so a second
    # read with one batch in flight ships the same range twice. A tailer that
    # reads ahead of its receipts by design says so and is never held.
    held_while_pending = not getattr(shipper.tailer, "reads_ahead", False)
    pending = agent.pending_batches
    while not agent.shutdown.is_set():
        wait_on_ack = False
        try:
            pending.prune_stale()
            # The hold keeps a held group at one batch in flight, so the release
            # it waits on below is always this batch's, never a stale one.
            if held_while_pending and pending.in_flight(group_name):
                wait_on_ack = True
            else:
                wait_on_ack = await _ship_one(
                    agent, ws, shipper, group_name, resolver_provider
                )
        except ConnectionClosed:
            raise
        except Exception:
            logger.warning("Log loop error for group %s", group_name, exc_info=True)

        # A full batch means a backlog: read again when its ACK moves the
        # Checkpoint, so a burst drains at wire speed instead of one batch an interval.
        if wait_on_ack:
            await _released_or_shutdown(agent, group_name, interval)
            continue
        if await sleep_or_shutdown(agent.shutdown, interval):
            return


async def _released_or_shutdown(agent: Agent, group: str, timeout: float) -> None:
    """Wait for ``group``'s batch to be ACKed or pruned, for shutdown, or ``timeout``."""
    waits = {
        asyncio.ensure_future(agent.pending_batches.wait_released(group)),
        asyncio.ensure_future(agent.shutdown.wait()),
    }
    _, unfinished = await asyncio.wait(
        waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
    )
    for task in unfinished:
        task.cancel()
    await asyncio.gather(*unfinished, return_exceptions=True)


async def _ship_one(
    agent: Agent,
    ws: ClientConnection,
    shipper: LogShipper,
    group_name: str,
    resolver_provider: Callable[[], LogEnricher | None],
) -> bool:
    """Collect and ship one batch; True when its read filled the cap."""
    started = time.monotonic()
    # Tick-fresh enrichment map; state is reassigned
    # atomically, so each tick sees a consistent view.
    resolver = resolver_provider()
    batch = await asyncio.to_thread(
        shipper.collect_batch,
        resolver,
        pending=agent.pending_batches.receipts(),
    )
    if batch is None:
        return False
    batch_id = str(uuid.uuid4())
    log_batch = LogBatchPayload(
        group=group_name,
        parser=shipper.parser_name,
        batch_id=batch_id,
        lines=batch.lines,
        dropped=batch.dropped,
        from_position=batch.from_position,
        to_position=batch.to_position,
    )
    envelope = make(agent.config.agent.id, log_batch)
    agent.pending_batches.add(batch_id, batch.receipt)
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
    return batch.more


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
                events_batch = EventsBatchPayload(batch_id=batch_id, events=batch)
                envelope = make(agent_id, events_batch)
                await ws.send(envelope.to_json())
                logger.debug("Shipped events.batch %s events=%d", batch_id, len(batch))
        except ConnectionClosed:
            raise
        except Exception:
            logger.warning("Events loop error", exc_info=True)
        if await sleep_or_shutdown(agent.shutdown, interval):
            return


async def event_spool_loop(agent: Agent, feeder: SpoolFeeder) -> None:
    """Feed one Integration's spool into the event buffer on the events cadence:
    the events loop ships what this reads, so a faster read only waits."""
    interval = agent.config.metrics.push_interval_seconds
    while not agent.shutdown.is_set():
        try:
            await asyncio.to_thread(feeder.tick)
        except Exception:
            logger.warning(
                "Event spool error for %s", feeder.source.name, exc_info=True
            )
        if await sleep_or_shutdown(agent.shutdown, interval):
            return
