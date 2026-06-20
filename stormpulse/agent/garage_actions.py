"""Garage-state side effects: the JobManager post-success refresh+push hook.

The on-demand ``garage_refresh`` ceremony moved to the generic, agent-owned
``stormpulse.agent.refresh`` (one routine for every state-collecting
integration). What stays here is genuinely garage-specific: the post-mutation
``on_success`` hook a garage job fires, plus the metrics-envelope builder it
shares with the generic refresh.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from stormpulse.agent.integrations_runtime import (
    IntegrationRuntime,
    build_integrations_payload,
)
from stormpulse.config import CommandSpec
from stormpulse.garage.state import collect_garage_state
from stormpulse.metrics import collect_metrics
from stormpulse.protocol import (
    Envelope,
    make_metrics_push,
)

if TYPE_CHECKING:
    from stormpulse.agent import Agent

logger = logging.getLogger(__name__)


def _live_garage(agent: Agent) -> IntegrationRuntime | None:
    """Return the garage Integration runtime iff it is live, else None.

    The post-mutation hook is garage-specific (the group=="garage" refresh+push),
    so it reaches the runtime by id.
    """
    rt = agent.integrations.get("garage")
    return rt if rt is not None and rt.status == "live" else None


def post_success_hook(
    agent: Agent,
    cmd_def: CommandSpec,
    command: str,
) -> Callable[[], Awaitable[None]] | None:
    """Build the after-success callback for a garage long-runner (immediate refresh+push), or ``None`` for non-garage."""
    if cmd_def.group != "garage":
        return None

    # Read-only garage commands (long-running provenance/stats reads) mutate
    # nothing, so a refresh+push after them is pure waste and floods state pushes.
    if cmd_def.read_only:
        return None

    # set-quota is the recompute's OWN action (BUCKETS-006). Refreshing + pushing
    # metrics after it re-enters the website recompute, which dispatches more
    # set-quotas, a runaway feedback loop (each set_quota fans out into more).
    # A quota change does not alter customer-visible usage either, so there is
    # nothing for the push to deliver. Skip the hook for it.
    if command == "garage_bucket_set_quota":
        return None

    async def refresh_and_push() -> None:
        if agent.job_manager is None:
            return
        await refresh_garage_state(agent)
        envelope = await build_metrics_envelope(agent)
        await agent.job_manager.send_now(envelope)
        logger.info("Sent post-mutation metrics push for %s", command)

    return refresh_and_push


async def refresh_garage_state(agent: Agent) -> None:
    """Collect a fresh Garage snapshot and store it on the garage runtime."""
    rt = _live_garage(agent)
    if rt is None:
        return
    state = await asyncio.to_thread(collect_garage_state, rt.config)
    if state is not None:
        rt.state = state


async def build_metrics_envelope(agent: Agent) -> Envelope:
    """Bundle host metrics + the latest Integration snapshots into a ``metrics.push``."""
    metrics = await asyncio.to_thread(collect_metrics, agent.config)
    integrations = build_integrations_payload(agent.integrations) or None
    return make_metrics_push(
        agent.config.agent.id,
        metrics,
        integrations=integrations,
    )
