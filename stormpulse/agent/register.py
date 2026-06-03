"""Initial register envelope sent immediately after a connection comes up.

The dashboard needs the agent's identity, version, command catalogue,
Garage state snapshot (if any), enabled log groups, and a best-effort
system inventory before it can route work to this agent. The seal flag
is re-stat'd here so an operator-driven seal between sessions
advertises promptly to the dashboard.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from websockets.asyncio.client import ClientConnection

from stormpulse import __version__
from stormpulse.agent.metadata import build_commands_metadata
from stormpulse.garage.discover import discover_garage
from stormpulse.protocol import make_register
from stormpulse.system_inventory import collect_system_inventory

if TYPE_CHECKING:
    from stormpulse.agent import Agent

logger = logging.getLogger(__name__)


async def send_register(agent: Agent, ws: ClientConnection, url: str) -> None:
    """Build and send the register envelope after a fresh connection."""
    logger.info("Connected to dashboard at %s", url)

    garage_dict = None
    if agent.config.garage and agent.config.garage.enabled:
        if agent.garage_disabled_reason is not None:
            # ADR GARAGE-000: precondition failed at bootstrap. The
            # disabled sentinel was seeded on Agent.__init__; ride it
            # to the dashboard verbatim, do not call discover_garage.
            if agent.garage_state is not None:
                garage_dict = agent.garage_state.to_dict()
        else:
            agent.garage_state = await asyncio.to_thread(
                discover_garage,
                agent.config.garage,
            )
            if agent.garage_state:
                garage_dict = agent.garage_state.to_dict()

    log_group_names = sorted(agent.shippers.keys()) or None
    system_inventory = (
        await asyncio.to_thread(
            collect_system_inventory,
        )
        or None
    )

    # Re-stat the seal file at register time so a fresh connect after
    # `stormpulse signoff seal` advertises the new state to the
    # dashboard immediately (see ADR CORE-004). The unsealed_since
    # timestamp rides alongside so the dashboard's "unsealed for X" /
    # "unsealed > N hours" pager has the authoritative wall-clock from
    # the agent rather than having to guess from its register history.
    sealed_now = agent.signoff_state.is_sealed()
    since = agent.signoff_state.unsealed_since()
    register = make_register(
        agent.config.agent.id,
        __version__,
        agent.config.agent.pulse_token,
        commands=build_commands_metadata(
            agent.registry,
            agent.config.project,
        ),
        garage=garage_dict,
        log_groups=log_group_names,
        system_inventory=system_inventory,
        signoff_sealed=sealed_now,
        unsealed_since=since.isoformat() if since is not None else None,
    )
    await ws.send(register.to_json())
    logger.info("Sent register (v%s)", __version__)
    if agent.pulse_logger is not None:
        agent.pulse_logger.info(
            "Connected to dashboard",
            "connection",
            {"url": url, "version": __version__},
        )
