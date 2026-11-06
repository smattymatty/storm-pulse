"""Send ``inventory.packages`` at session start and whenever dpkg's status file moves."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from stormpulse.agent.loops import sleep_or_shutdown
from stormpulse.protocol import InventoryPackagesPayload, make
from stormpulse.system_inventory import collect_source_packages

if TYPE_CHECKING:
    from stormpulse.agent import Agent

logger = logging.getLogger(__name__)

DPKG_STATUS = Path("/var/lib/dpkg/status")
POLL_INTERVAL_SECONDS = 300.0


def _mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


async def _send_once(
    ws: ClientConnection,
    agent_id: str,
    collect: Callable[[], InventoryPackagesPayload | None],
) -> bool:
    """Collect off the loop and send; True only when a full list went out."""
    payload = await asyncio.to_thread(collect)
    if payload is None:
        return False
    try:
        await ws.send(make(agent_id, payload).to_json())
    except ConnectionClosed:
        raise
    except Exception:
        logger.warning("Failed to send inventory.packages", exc_info=True)
        return False
    logger.info("Sent %d source packages", len(payload.packages))
    return True


async def packages_push_loop(
    agent: Agent,
    ws: ClientConnection,
    *,
    status_path: Path = DPKG_STATUS,
    collect: Callable[[], InventoryPackagesPayload | None] = collect_source_packages,
) -> None:
    """Send the package list once per session, then again each time dpkg changes.

    The mtime is read before collecting, so a change mid-collect sends again.
    Only a sent list advances the baseline; a failed collect or send retries.
    """
    agent_id = agent.config.agent.id
    sent_mtime: int | None = None
    while True:
        mtime = _mtime(status_path)
        if mtime is not None and mtime != sent_mtime:
            if await _send_once(ws, agent_id, collect):
                sent_mtime = mtime
        if await sleep_or_shutdown(agent.shutdown, POLL_INTERVAL_SECONDS):
            return
