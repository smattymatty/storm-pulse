"""Outer connect / run-tasks / reconnect-with-backoff loop. ``run_with_backoff`` is what ``Agent.run`` calls."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from stormpulse.agent import dispatch, loops
from stormpulse.agent.register import send_register
from stormpulse.agent.signoff_nag import signoff_nag_loop
from stormpulse.agent.signoff_push import signoff_state_push_loop
from stormpulse.commands.jobs import JobManager
from stormpulse.protocol import Envelope

if TYPE_CHECKING:
    from stormpulse.agent import Agent

logger = logging.getLogger(__name__)


async def run_with_backoff(agent: Agent) -> None:
    """Connect, run the per-session task group, reconnect on failure (1.5× backoff + 25% jitter, clamped)."""
    url = agent.config.dashboard.url
    delay = agent.config.dashboard.reconnect_min_seconds
    attempts = 0
    first_failure_at: float | None = None

    while not agent.shutdown.is_set():
        try:
            attempts += 1
            if attempts == 1:
                logger.info("Connecting to %s", url)
            else:
                since = (
                    f", {time.monotonic() - first_failure_at:.0f}s since first failure"
                    if first_failure_at is not None
                    else ""
                )
                logger.info(
                    "Connecting to %s (attempt %d%s)",
                    url,
                    attempts,
                    since,
                )
            async with connect(
                url,
                ssl=agent._ssl_ctx,
                open_timeout=10,
                ping_interval=20,
                ping_timeout=20,
                compression=None,
            ) as ws:
                await _run_session(agent, ws, url)
                # Clean return = registered at least once; reset attempt tracking.
                attempts = 0
                first_failure_at = None
                delay = agent.config.dashboard.reconnect_min_seconds
        except* ConnectionClosed as eg:
            if first_failure_at is None:
                first_failure_at = time.monotonic()
            _log_connection_event(
                agent,
                "Connection closed",
                "warning",
                eg.exceptions[0],
            )
        except* OSError as eg:
            if first_failure_at is None:
                first_failure_at = time.monotonic()
            _log_connection_event(
                agent,
                "Connection error",
                "warning",
                eg.exceptions[0],
            )
        except* Exception as eg:
            if first_failure_at is None:
                first_failure_at = time.monotonic()
            _log_connection_event(
                agent,
                "Unexpected error",
                "error",
                eg.exceptions[0],
                category="error",
            )

        if agent.shutdown.is_set():
            break

        delay = await _sleep_with_jitter(agent, delay)

    for tailer in agent.streaming_tailers:
        tailer.close()
    logger.info("Agent shutting down")


async def _run_session(
    agent: Agent,
    ws: ClientConnection,
    url: str,
) -> None:
    """Run one connection's worth of work: register, then the task group."""
    await send_register(agent, ws, url)

    async def ws_send(env: Envelope) -> None:
        await ws.send(env.to_json())

    agent_id = agent.config.agent.id
    agent.job_manager = JobManager(agent_id, ws_send)
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(_shutdown_watcher(agent, ws))
            tg.create_task(loops.heartbeat_loop(agent, ws))
            tg.create_task(loops.metrics_loop(agent, ws))
            tg.create_task(dispatch.receive_loop(agent, ws))
            # Per live Integration: a periodic state loop (if it collects state)
            # and a fast new-resource detector (if it declares one). caddy
            # declares neither, so no loop spins up for it (CORE-005).
            for integ_id, rt in agent.integrations.items():
                if rt.status != "live":
                    continue
                if rt.descriptor.collect_state is not None:
                    tg.create_task(loops.integration_state_loop(agent, ws, integ_id))
                if rt.descriptor.detect is not None:
                    tg.create_task(loops.integration_detect_loop(agent, ws, integ_id))
            tg.create_task(signoff_nag_loop(agent, ws))
            tg.create_task(signoff_state_push_loop(agent, ws))
            for group_name in agent.shippers:
                tg.create_task(loops.log_loop(agent, ws, group_name))
    finally:
        await agent.job_manager.shutdown_all()
        agent.job_manager = None


async def _shutdown_watcher(agent: Agent, ws: ClientConnection) -> None:
    """Close the websocket on shutdown so ``recv()`` unblocks (otherwise systemd SIGKILLs us at ``TimeoutStopSec``)."""
    await agent.shutdown.wait()
    await ws.close()


async def _sleep_with_jitter(agent: Agent, delay: float) -> float:
    """Wait *delay* (+25% jitter); return the next exponentially-backed-off delay, clamped to ``reconnect_max_seconds``."""
    jitter = random.uniform(0, delay * 0.25)
    wait = delay + jitter
    logger.info("Reconnecting in %.1fs", wait)
    try:
        await asyncio.wait_for(agent.shutdown.wait(), timeout=wait)
    except TimeoutError:
        pass
    return min(delay * 1.5, agent.config.dashboard.reconnect_max_seconds)


def _log_connection_event(
    agent: Agent,
    message: str,
    level: str,
    exc: BaseException,
    *,
    category: str = "connection",
) -> None:
    """Log a connection lifecycle event to both the std logger and PulseLogger."""
    if level == "error":
        logger.error("%s: %s", message, exc, exc_info=True)
    else:
        logger.warning("%s: %s", message, exc)
    if agent.pulse_logger is None:
        return
    log_fn = getattr(agent.pulse_logger, level)
    log_fn(message, category, {"reason": str(exc)})
