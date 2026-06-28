"""Garage-state side effects on the agent runtime: the JobManager post-success hook.

The on-demand ``garage_refresh`` ceremony moved to the generic, agent-owned
``stormpulse.agent.refresh`` (one routine for every state-collecting
integration). What stays here is the agent-side orchestration a garage mutation
needs: the post-success ``on_success`` hook that targeted-re-reads the touched
buckets and pushes, the race-discipline merge into runtime state, and the
metrics-envelope builder it shares with the generic refresh. The garage-domain
read planning (which buckets a mutation touched) and execution live in
``stormpulse.garage.state`` (``affected_bucket_ids`` / ``read_buckets_by_id``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING

from stormpulse.agent.integrations_runtime import (
    IntegrationRuntime,
    build_integrations_payload,
)
from stormpulse.config import CommandSpec
from stormpulse.garage.state import (
    GarageBucket,
    GarageState,
    affected_bucket_ids,
    cap_targeted_reads,
    read_buckets_by_id,
)
from stormpulse.metrics import collect_metrics
from stormpulse.protocol import (
    Envelope,
    make_metrics_push,
)

if TYPE_CHECKING:
    from stormpulse.agent import Agent

logger = logging.getLogger(__name__)


def merge_buckets_into_runtime(
    rt: IntegrationRuntime,
    buckets: list[GarageBucket],
) -> bool:
    """Upsert *buckets* into ``rt.state`` atomically. Returns False if no baseline.

    The race discipline made executable. Three loops write ``rt.state`` - the
    new-bucket detector, the post-mutation hook, and the per-bucket usage walk -
    so the hazard is **lost-update-across-await**: a writer that captured
    ``rt.state`` BEFORE its admin ``await``s and merged into that stale snapshot
    afterward would silently drop another writer's concurrent change, and a
    dropped bucket reads downstream as a deletion (manifest alarms, never acts).

    The discipline this enforces: do ALL admin I/O FIRST, leaving the freshly
    read bucket(s) in locals, THEN call this helper. It is fully synchronous -
    there is no ``await`` between reading ``rt.state`` and assigning it - so the
    event loop cannot interleave another writer between the read and the store.
    No lost update, no Lock. NEVER capture ``rt.state`` before an ``await`` and
    pass a snapshot built from that capture in here; that reintroduces the bug.

    Returns False when no baseline state exists yet (a cold start before the
    first full collect): a targeted merge has no full snapshot to upsert into, so
    the caller defers to the next full collect rather than synthesizing an
    invalid partial-manifest snapshot.
    """
    state: GarageState | None = rt.state
    if state is None:
        return False
    rt.state = state.with_buckets(buckets)
    return True


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
    params: Mapping[str, str],
) -> Callable[[], Awaitable[None]] | None:
    """Build the after-success callback for a garage mutation (targeted re-read + push), or ``None``.

    Returns None for non-garage commands, read-only garage long-runners (which
    mutate nothing, so a push would only flood the dashboard), and self-reconciling
    commands (dispatched repeatedly by a reconciliation loop, so no single success
    is the "did it land" moment a push would serve - the periodic walk reflects
    them each cycle). Both gates read a ``CommandSpec`` flag set at the spec, the
    same way ``read_only`` is, so the agent layer never hardcodes garage command
    names. Everything else gets a callback that re-reads only the buckets this
    command's ``params`` identify and pushes the merged snapshot - never a full sweep.
    """
    if cmd_def.group != "garage":
        return None
    if cmd_def.read_only or cmd_def.self_reconciling:
        return None

    async def refresh_and_push() -> None:
        if agent.job_manager is None:
            return
        # No affected bucket read back (new-bucket op, alias-only op, a delete's
        # 404, or no baseline yet): nothing to push; the periodic walk reflects it.
        if not await refresh_affected_buckets(agent, params):
            return
        envelope = await build_metrics_envelope(agent)
        await agent.job_manager.send_now(envelope)
        logger.info("Sent post-mutation metrics push for %s", command)

    return refresh_and_push


async def refresh_affected_buckets(agent: Agent, params: Mapping[str, str]) -> bool:
    """Re-read only the buckets a mutation touched and merge them; True iff any merged.

    The targeted heir of the old full-sweep refresh. It resolves the affected ids
    from the command's ``params`` plus the current snapshot, bounds the fan-out via
    ``cap_targeted_reads`` (a key may touch many buckets), re-reads ONLY those over
    the admin API, and upserts them. Returns False - no push - when
    nothing is affected, there is no baseline yet, or the affected buckets did not
    read back (e.g. a delete's 404). Removals are never expressed here (upsert
    only, manifest alarms never acts); they ride the periodic walk + reconcile.

    Race discipline: the snapshot only chooses *which* ids to re-read; the admin
    I/O runs first, then ``merge_buckets_into_runtime`` reads the *current*
    ``rt.state`` and assigns in one await-free step, so a concurrent writer's
    change is never lost.
    """
    rt = _live_garage(agent)
    if rt is None:
        return False
    state = rt.state
    if state is None:
        return False
    ids = affected_bucket_ids(params, state)
    if not ids:
        return False
    capped = cap_targeted_reads(ids, context="Post-mutation")
    buckets = await asyncio.to_thread(read_buckets_by_id, rt.config, capped)
    if not buckets:
        return False
    return merge_buckets_into_runtime(rt, buckets)


async def build_metrics_envelope(agent: Agent) -> Envelope:
    """Bundle host metrics + the latest Integration snapshots into a ``metrics.push``."""
    metrics = await asyncio.to_thread(collect_metrics, agent.config)
    integrations = build_integrations_payload(agent.integrations) or None
    return make_metrics_push(
        agent.config.agent.id,
        metrics,
        integrations=integrations,
    )
