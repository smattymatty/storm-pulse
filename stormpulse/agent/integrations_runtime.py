"""Per-Integration runtime state, the shared targeted merge, and the one
metrics-envelope builder (CORE-005; heir of the named garage_live/garage_state pair)."""

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
    """Per-process runtime for one configured Integration: wire status + reason,
    parsed config, contract descriptor, and the latest collected state blob."""

    id: str
    status: str
    disabled_reason: str | None
    config: Any
    descriptor: Integration
    state: StateBlob | None = None


def build_integrations_payload(
    runtimes: dict[str, IntegrationRuntime],
) -> dict[str, dict[str, Any]]:
    """Build the ``integrations`` wire envelope: live carries its state blob ({} until
    first collect), disabled carries null; reason non-null iff disabled_error."""
    out: dict[str, dict[str, Any]] = {}
    for integ_id, runtime in runtimes.items():
        if runtime.status == STATUS_LIVE:
            state: dict[str, Any] | None = (
                runtime.state.to_dict() if runtime.state is not None else {}
            )
        else:
            state = None
        out[integ_id] = {
            "status": runtime.status,
            "disabled_reason": runtime.disabled_reason,
            "state": state,
        }
    return out


def merge_items_into_runtime(runtime: IntegrationRuntime, items: list[Any]) -> bool:
    """Upsert *items* into the CURRENT ``runtime.state`` atomically; False when no
    baseline yet (a cold start defers to the next full collect). CORE-005 decision 11."""
    state = runtime.state
    if state is None:
        return False
    if not isinstance(state, MergeableState):
        raise TypeError(
            f"Integration {runtime.id!r} declares a targeted writer (detect/read_affected) "
            "but its state type has no with_items() (MergeableState)"
        )
    # Read-merge-assign with no await between, so a concurrent writer is never
    # dropped. NEVER merge into a snapshot captured before an await (decision 11).
    runtime.state = state.with_items(items)
    return True


async def build_metrics_envelope(agent: Agent) -> Envelope:
    """Bundle host metrics, Integration snapshots, and job load into one ``metrics.push``.
    The single builder for every push trigger, so the envelope shape cannot drift."""
    metrics = await asyncio.to_thread(collect_metrics, agent.config)
    integrations = build_integrations_payload(agent.integrations) or None
    job_load = agent.job_manager.load() if agent.job_manager else None
    return make_metrics_push(
        agent.config.agent.id,
        metrics,
        integrations=integrations,
        job_load=job_load,
    )
