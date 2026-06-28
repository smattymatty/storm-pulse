"""Periodic per-connection task bodies (heartbeat, metrics, integration state, log shipping) plus shared ``sleep_or_shutdown``."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import TYPE_CHECKING

from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from stormpulse.agent.garage_actions import (
    build_metrics_envelope,
    merge_buckets_into_runtime,
)
from stormpulse.agent.integrations_runtime import build_integrations_payload
from stormpulse.garage.bucket_resolver import BucketIdResolver
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
            integrations = build_integrations_payload(agent.integrations) or None
            envelope = make_metrics_push(agent_id, metrics, integrations=integrations)
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
    """Refresh one live Integration's state on the metrics-push cadence (CORE-005).

    Generic heir of ``garage_loop``: it calls the Integration's own
    ``collect_state`` and stores the result on its runtime, where ``metrics_loop``
    bundles it into the wire. Spun up only for Integrations that declare
    ``collect_state`` (see reconnect), so caddy never enters here.

    State rides the metrics push, so collection runs at exactly the push
    interval: a faster collect is discarded work that still pays full backend
    cost (the 2026-06-27 saturation incident). An Integration needing different
    freshness for different data composes its own sub-cadences internally (garage
    does, behind ``GarageStateReader``), never a per-Integration interval knob.
    """
    rt = agent.integrations[integ_id]
    desc = rt.descriptor
    assert desc.collect_state is not None
    interval = agent.config.metrics.push_interval_seconds
    while not agent.shutdown.is_set():
        try:
            state = await asyncio.to_thread(desc.collect_state, rt.config)
            if state is not None:
                rt.state = state
                logger.debug("Refreshed %s state", integ_id)
        except Exception:
            logger.warning("Failed to collect %s state", integ_id, exc_info=True)
        if await sleep_or_shutdown(agent.shutdown, interval):
            return


async def integration_detect_loop(
    agent: Agent, ws: ClientConnection, integ_id: str
) -> None:
    """Run one Integration's fast new-resource detector (capacity-model 2026-06-27 amendment).

    Spun up only for Integrations declaring a ``Detector`` (see reconnect), at the
    detector's own interval - the one tunable state-read cadence, the security
    dial bounding the window in which an out-of-band-created resource is uncapped. The detector is constant-cost (a single list call); on a
    newcomer it merges the new resource(s) into the current snapshot and pushes
    immediately, so a fresh resource reaches the control plane within one
    detector interval rather than waiting for the slower periodic walk.

    Merge and push name the garage runtime helpers directly, as ``log_loop`` does
    for log enrichment: detection is, in practice, the garage new-bucket signal.
    The race discipline is preserved by ``merge_buckets_into_runtime``, which
    reads the *current* ``rt.state`` after the admin await and assigns in one
    synchronous step. The snapshot handed to ``detect`` is only the diff baseline,
    and re-merging an already-known resource is an idempotent upsert, so a bucket
    that another writer added mid-await is merged correctly, never lost.
    """
    rt = agent.integrations[integ_id]
    detector = rt.descriptor.detect
    assert detector is not None
    interval = detector.interval(rt.config)
    while not agent.shutdown.is_set():
        try:
            newcomers = await asyncio.to_thread(detector.run, rt.config, rt.state)
            if newcomers and merge_buckets_into_runtime(rt, newcomers):
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
            # garage-state snapshot the refresh loop published (BUCKETS-015).
            # Log enrichment is inherently garage-specific, so this names the
            # garage runtime directly; rt.state is reassigned atomically, so the
            # captured snapshot is a consistent, immutable view for this tick.
            garage_rt = agent.integrations.get("garage")
            garage_state = garage_rt.state if garage_rt is not None else None
            resolver = BucketIdResolver.from_state(garage_state)
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
