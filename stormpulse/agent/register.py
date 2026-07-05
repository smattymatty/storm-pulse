"""Initial ``register`` envelope sent on every fresh connection; carries identity, command surface, integration snapshots, and seal state."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from websockets.asyncio.client import ClientConnection

from stormpulse import __version__
from stormpulse.agent.integrations_runtime import build_integrations_payload
from stormpulse.agent.metadata import build_commands_metadata
from stormpulse.protocol import make_register
from stormpulse.system_inventory import collect_system_inventory

if TYPE_CHECKING:
    from stormpulse.agent import Agent

logger = logging.getLogger(__name__)


async def send_register(agent: Agent, ws: ClientConnection, url: str) -> None:
    """Build and send the register envelope after a fresh connection."""
    logger.info("Connected to dashboard at %s", url)

    # CORE-005: discover initial state for each live Integration that declares a
    # discover capability and has no state yet (garage discovers here).
    for runtime in agent.integrations.values():
        if (
            runtime.status == "live"
            and runtime.descriptor.discover is not None
            and runtime.state is None
        ):
            runtime.state = await asyncio.to_thread(runtime.descriptor.discover, runtime.config)
    integrations = build_integrations_payload(agent.integrations) or None

    log_group_names = sorted(agent.shippers.keys()) or None
    system_inventory = (
        await asyncio.to_thread(
            collect_system_inventory,
        )
        or None
    )

    # Re-stat seal at register time so post-seal connects advertise promptly (ADR CORE-004).
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
        integrations=integrations,
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
