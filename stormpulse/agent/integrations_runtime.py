"""Per-Integration runtime state, the shared targeted merge, and the one
metrics-envelope builder (CORE-005). Two writers touch ``runtime.state``: a
collect REPLACES it through ``replace_runtime_state`` (the reader composes its
own sub-cadences); the post-mutation hook PATCHES already-known resources
through ``merge_items_into_runtime``."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field, replace
from typing import TYPE_CHECKING, Any

from stormpulse.integrations import FreshState, Integration, MergeableState, StateBlob
from stormpulse.metrics import collect_metrics
from stormpulse.protocol import (
    Component,
    Envelope,
    IntegrationReport,
    make_metrics_push,
)
from stormpulse.sdk import SdkComponent

if TYPE_CHECKING:
    from stormpulse.agent import Agent

logger = logging.getLogger(__name__)

# Retain only watermarks, never one resource list per outstanding push.
MAX_PENDING_METRICS_PUSHES = 32

# Status values, per core-005-integrations-wire.md.
STATUS_LIVE = "live"
STATUS_DISABLED_ERROR = "disabled_error"
STATUS_DISABLED_CHOICE = "disabled_choice"


@dataclass(slots=True)
class IntegrationRuntime:
    """Per-process runtime for one configured Integration: wire status + reason,
    parsed config, contract descriptor, the latest collected state blob, the
    command receipt (the digest a grant pins, why commands are fenced) and the
    push envelope (CORE-011 d5), which lives here so it survives a reconnect."""

    id: str
    status: str
    disabled_reason: str | None
    config: Any
    descriptor: Integration
    state: StateBlob | None = None
    command_specs_digest: str = ""
    commands_fenced: str | None = None
    generation: int = 0
    complete: bool = True
    last_full_read_at: str = ""
    # Private read revisions, separate from wire push generation. Keys are the
    # ids owed since the last ack, in first-seen order; a lost push repeats them.
    change_revision: int = 0
    changed_revisions: dict[str, int] = field(default_factory=dict)
    pending_pushes: dict[str, int] = field(default_factory=dict)


def build_integrations_payload(
    runtimes: dict[str, IntegrationRuntime],
) -> dict[str, dict[str, Any]]:
    """Build the ``integrations`` wire envelope, one ``IntegrationReport`` per id:
    live carries its state blob ({} until first collect), disabled carries null;
    reason non-null iff disabled_error."""
    out: dict[str, dict[str, Any]] = {}
    for integ_id, runtime in runtimes.items():
        if runtime.status == STATUS_LIVE:
            state: dict[str, Any] | None = (
                runtime.state.to_dict() if runtime.state is not None else {}
            )
        else:
            state = None
        out[integ_id] = asdict(
            IntegrationReport(
                status=runtime.status,
                disabled_reason=runtime.disabled_reason,
                state=state,
                command_specs_digest=runtime.command_specs_digest,
                commands_fenced=runtime.commands_fenced,
                generation=runtime.generation,
                complete=runtime.complete,
                last_full_read_at=runtime.last_full_read_at,
                changed_ids=list(runtime.changed_revisions),
            )
        )
    return out


def replace_runtime_state(runtime: IntegrationRuntime, state: StateBlob) -> None:
    """A collect REPLACES ``runtime.state`` and folds what it says of itself into
    the envelope; a blob without ``freshness()`` reads as complete and unchanged."""
    runtime.state = state
    if not isinstance(state, FreshState):
        return
    fresh = state.freshness()
    runtime.complete = fresh.complete
    runtime.last_full_read_at = fresh.last_full_read_at
    _note_changed(runtime, fresh.changed_ids)


def _note_changed(runtime: IntegrationRuntime, ids: Any) -> None:
    """Owe every read, including a newer read of an already-dirty resource."""
    for item_id in ids:
        if item_id:
            runtime.change_revision += 1
            runtime.changed_revisions[item_id] = runtime.change_revision


def advance_generation(runtimes: dict[str, IntegrationRuntime]) -> None:
    """Advance the wire push generation, without changing read revisions."""
    for runtime in runtimes.values():
        runtime.generation += 1


def track_push(runtimes: dict[str, IntegrationRuntime], push_id: str) -> None:
    """Capture each snapshot's read watermark; eviction never clears changes."""
    for runtime in runtimes.values():
        runtime.pending_pushes[push_id] = runtime.change_revision
        while len(runtime.pending_pushes) > MAX_PENDING_METRICS_PUSHES:
            del runtime.pending_pushes[next(iter(runtime.pending_pushes))]


def discard_push(runtimes: dict[str, IntegrationRuntime], push_id: str) -> None:
    """A failed send cannot be acknowledged; keep all dirty resources."""
    for runtime in runtimes.values():
        runtime.pending_pushes.pop(push_id, None)


def discard_pending_pushes(runtimes: dict[str, IntegrationRuntime]) -> None:
    """Retire connection-scoped receipts while preserving process-scoped changes."""
    for runtime in runtimes.values():
        runtime.pending_pushes.clear()


def acknowledge_push(runtimes: dict[str, IntegrationRuntime], push_id: str) -> bool:
    """Release only reads included in this push; newer reads stay dirty."""
    matched = False
    for runtime in runtimes.values():
        watermark = runtime.pending_pushes.pop(push_id, None)
        if watermark is None:
            continue
        matched = True
        runtime.changed_revisions = {
            item_id: revision
            for item_id, revision in runtime.changed_revisions.items()
            if revision > watermark
        }
    return matched


def integration_components(runtimes: dict[str, IntegrationRuntime]) -> list[Component]:
    """What each live Integration declares for ``register.components`` (CORE-011 d7).
    A hook that raises, or returns anything but SdkComponents of three strings,
    costs its own entries, never the register: the hook is ungated adapter code."""
    out: list[Component] = []
    for runtime in runtimes.values():
        hook = runtime.descriptor.components
        if runtime.status != STATUS_LIVE or hook is None:
            continue
        try:
            declared = [_component(c) for c in hook(runtime.config, runtime.state)]
        except Exception:  # noqa: BLE001 - read-only hook; a bad one is logged, not fatal
            logger.warning(
                "Integration %r: components hook failed", runtime.id, exc_info=True
            )
            continue
        out.extend(declared)
    return out


def _component(declared: object) -> Component:
    """One hook entry as the wire's ``Component``; TypeError on any other shape."""
    if not isinstance(declared, SdkComponent) or not all(
        isinstance(v, str) for v in (declared.name, declared.version, declared.digest)
    ):
        raise TypeError(
            f"components hook returned {declared!r}, not an SdkComponent of strings"
        )
    return Component(declared.name, declared.version, declared.digest)


def merge_items_into_runtime(runtime: IntegrationRuntime, items: list[Any]) -> bool:
    """Upsert *items* into the CURRENT ``runtime.state`` atomically; False when no
    baseline yet (a cold start defers to the next full collect). CORE-005 decision 11."""
    state = runtime.state
    if state is None:
        return False
    if not isinstance(state, MergeableState):
        raise TypeError(
            f"Integration {runtime.id!r} declares a targeted writer (read_affected) "
            "but its state type has no with_items() (MergeableState)"
        )
    # Read-merge-assign with no await between, so a concurrent writer is never
    # dropped. NEVER merge into a snapshot captured before an await (decision 11).
    runtime.state = state.with_items(items)
    _note_changed(runtime, (getattr(item, "id", "") for item in items))
    return True


async def build_metrics_envelope(agent: Agent) -> Envelope:
    """Bundle host metrics, Integration snapshots, and job load into one ``metrics.push``.
    The single builder for every push trigger, so the envelope shape cannot drift."""
    metrics = await asyncio.to_thread(collect_metrics, agent.config)
    advance_generation(agent.integrations)
    # Reports ride as their ``asdict`` already, the same loose row ``make_register`` takes.
    integrations: dict[str, Any] | None = (
        build_integrations_payload(agent.integrations) or None
    )
    metrics = replace(
        metrics,
        integrations=integrations,
        jobs=agent.job_manager.load() if agent.job_manager else None,
    )
    envelope = make_metrics_push(agent.config.agent.id, metrics)
    track_push(agent.integrations, envelope.id)
    return envelope
