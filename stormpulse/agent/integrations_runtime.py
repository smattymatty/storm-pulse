"""Generic per-Integration runtime state, the shared merge, and the metrics envelope.

Replaces the agent's old named ``garage_live`` / ``garage_state`` pair with one
``IntegrationRuntime`` per configured Integration, plus the builder that turns
the runtime set into the ``integrations`` payload the spec pins, the targeted
merge primitive every state writer shares, and the one ``metrics.push`` builder.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from stormpulse.integrations import Integration, MergeableState, StateBlob
from stormpulse.metrics import collect_metrics
from stormpulse.protocol import Envelope, make_metrics_push

if TYPE_CHECKING:
    from stormpulse.agent import Agent

# Status values, per core-005-integrations-wire.md.
STATUS_LIVE = "live"
STATUS_DISABLED_ERROR = "disabled_error"
STATUS_DISABLED_CHOICE = "disabled_choice"


@dataclass(slots=True)
class IntegrationRuntime:
    """Per-process runtime state for one configured Integration.

    ``status`` + ``disabled_reason`` drive the wire envelope; ``descriptor``
    reaches the runtime capabilities (discover, collect_state, interval);
    ``state`` holds the latest collected blob, reassigned by the refresh loop
    (the generic heir of the old ``agent.garage_state`` reassignment).
    """

    id: str
    status: str
    disabled_reason: str | None
    config: Any
    descriptor: Integration
    state: StateBlob | None = None


def build_integrations_payload(
    runtimes: dict[str, IntegrationRuntime],
) -> dict[str, dict[str, Any]]:
    """Build the CORE-005 ``integrations`` envelope from the runtime set.

    Per the spec: a live Integration carries its state blob (``{}`` until the
    first collection lands); a disabled one carries ``state: null``;
    ``disabled_reason`` is non-null iff ``status == disabled_error``.
    """
    out: dict[str, dict[str, Any]] = {}
    for integ_id, rt in runtimes.items():
        if rt.status == STATUS_LIVE:
            state: dict[str, Any] | None = (
                rt.state.to_dict() if rt.state is not None else {}
            )
        else:
            state = None
        out[integ_id] = {
            "status": rt.status,
            "disabled_reason": rt.disabled_reason,
            "state": state,
        }
    return out


def merge_items_into_runtime(rt: IntegrationRuntime, items: list[Any]) -> bool:
    """Upsert *items* into ``rt.state`` atomically. Returns False if no baseline.

    The race discipline made executable, shared by every targeted writer (the
    detect loop and the post-mutation hook). The hazard is
    **lost-update-across-await**: a writer that captured ``rt.state`` BEFORE its
    admin ``await``s and merged into that stale snapshot afterward would silently
    drop another writer's concurrent change, and a dropped item reads downstream
    as a deletion (manifest alarms, never acts).

    The discipline this enforces: do ALL admin I/O FIRST, leaving the freshly
    read item(s) in locals, THEN call this helper. It is fully synchronous -
    there is no ``await`` between reading ``rt.state`` and assigning it - so the
    event loop cannot interleave another writer between the read and the store.
    No lost update, no Lock. NEVER capture ``rt.state`` before an ``await`` and
    pass a snapshot built from that capture in here; that reintroduces the bug.

    Returns False when no baseline state exists yet (a cold start before the
    first full collect): a targeted merge has no full snapshot to upsert into, so
    the caller defers to the next full collect rather than synthesizing an
    invalid partial-manifest snapshot.
    """
    state = rt.state
    if state is None:
        return False
    if not isinstance(state, MergeableState):
        raise TypeError(
            f"Integration {rt.id!r} declares a targeted writer (detect/read_affected) "
            "but its state type has no with_items() (MergeableState)"
        )
    rt.state = state.with_items(items)
    return True


async def build_metrics_envelope(agent: Agent) -> Envelope:
    """Bundle host metrics, Integration snapshots, and job load into one ``metrics.push``.

    The single builder for every push - periodic, post-mutation, detect, and
    on-demand refresh - so the envelope shape cannot drift between triggers.
    """
    metrics = await asyncio.to_thread(collect_metrics, agent.config)
    integrations = build_integrations_payload(agent.integrations) or None
    job_load = agent.job_manager.load() if agent.job_manager else None
    return make_metrics_push(
        agent.config.agent.id,
        metrics,
        integrations=integrations,
        job_load=job_load,
    )
