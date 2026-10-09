"""Unit tests for the generic Integration runtime helpers.

The one metrics-envelope builder (every push trigger shares it) and the
``rt.state`` targeted-merge primitive (the race discipline).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from unittest.mock import MagicMock, patch

import pytest

from stormpulse.agent import Agent
from stormpulse.agent.integrations_runtime import (
    IntegrationRuntime,
    acknowledge_push,
    build_metrics_envelope,
    merge_items_into_runtime,
    replace_runtime_state,
)
from stormpulse.commands.jobs import JobManager
from stormpulse.garage.integration import GARAGE_INTEGRATION
from stormpulse.garage.state import GarageState
from stormpulse.integrations import Freshness
from stormpulse.protocol import Envelope
from stormpulse.sdk import SdkComponent
from tests.helpers import (
    FAKE_METRICS,
    make_fake_garage_state,
    make_garage_bucket,
    set_garage_state,
)

# ---------------------------------------------------------------------------
# build_metrics_envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("stormpulse.agent.integrations_runtime.collect_metrics")
async def test_build_metrics_envelope_without_integrations(
    mock_collect: MagicMock,
    agent: Agent,
) -> None:
    """With no configured integrations the envelope omits the integrations key."""
    mock_collect.return_value = FAKE_METRICS
    envelope = await build_metrics_envelope(agent)
    assert envelope.payload["integrations"] is None
    assert envelope.payload["cpu_percent"] == FAKE_METRICS.cpu_percent


@pytest.mark.asyncio
@patch("stormpulse.agent.integrations_runtime.collect_metrics")
async def test_build_metrics_envelope_includes_garage_snapshot(
    mock_collect: MagicMock,
    agent_with_garage: Callable[..., Agent],
) -> None:
    """A live garage runtime's state rides under integrations.garage.state."""
    mock_collect.return_value = FAKE_METRICS
    ag = agent_with_garage()
    set_garage_state(ag, make_fake_garage_state())
    envelope = await build_metrics_envelope(ag)
    garage_report = envelope.payload["integrations"]["garage"]
    assert garage_report["status"] == "live"
    assert garage_report["state"]["node_id"] == "n1"


@pytest.mark.asyncio
@patch("stormpulse.agent.integrations_runtime.collect_metrics")
async def test_build_metrics_envelope_carries_job_load(
    mock_collect: MagicMock,
    agent: Agent,
) -> None:
    """Job load rides EVERY push, not just the periodic one (no builder drift)."""
    mock_collect.return_value = FAKE_METRICS

    async def _send(env: Envelope) -> None:
        return None

    agent.job_manager = JobManager(agent.config.agent.id, _send)
    envelope = await build_metrics_envelope(agent)
    assert envelope.payload["jobs"] is not None
    await agent.job_manager.shutdown_all()


# ---------------------------------------------------------------------------
# merge_items_into_runtime (the rt.state race discipline)
# ---------------------------------------------------------------------------

_MERGE_ID_A = "aaaa000000000000" + "0" * 48
_MERGE_ID_B = "bbbb000000000000" + "0" * 48


def _runtime(state: GarageState | None) -> IntegrationRuntime:
    return IntegrationRuntime(
        id="garage",
        status="live",
        disabled_reason=None,
        config=None,
        descriptor=GARAGE_INTEGRATION,
        state=state,
    )


def test_merge_into_runtime_returns_false_without_baseline() -> None:
    """No baseline state yet (cold start): the targeted merge defers, state stays None."""
    rt = _runtime(None)
    assert merge_items_into_runtime(rt, [make_garage_bucket(_MERGE_ID_A)]) is False
    assert rt.state is None


def test_merge_into_runtime_upserts_and_reassigns() -> None:
    prior = make_fake_garage_state()  # buckets=[]
    rt = _runtime(prior)
    ok = merge_items_into_runtime(rt, [make_garage_bucket(_MERGE_ID_A, size_bytes=7)])
    assert ok is True
    # rt.state is a NEW object (frozen merge), not the prior one mutated.
    assert rt.state is not prior
    assert isinstance(rt.state, GarageState)
    assert [b.id for b in rt.state.buckets] == [_MERGE_ID_A]
    assert rt.state.buckets[0].size_bytes == 7


def test_merge_into_runtime_preserves_full_manifest() -> None:
    """Merging a newcomer keeps every already-known bucket (manifest, not partial)."""
    base = make_fake_garage_state()
    rt = _runtime(base.with_items([make_garage_bucket(_MERGE_ID_A)]))
    merge_items_into_runtime(rt, [make_garage_bucket(_MERGE_ID_B)])
    assert isinstance(rt.state, GarageState)
    assert {b.id for b in rt.state.buckets} == {_MERGE_ID_A, _MERGE_ID_B}


class _UnmergeableState:
    def to_dict(self) -> dict[str, object]:
        return {}


def test_merge_into_runtime_rejects_unmergeable_state() -> None:
    """A targeted writer whose state type lacks with_items() fails loudly, never silently."""
    rt = _runtime(None)
    rt.state = _UnmergeableState()
    with pytest.raises(TypeError, match="MergeableState"):
        merge_items_into_runtime(rt, [make_garage_bucket(_MERGE_ID_A)])


# ---------------------------------------------------------------------------
# garage's components hook (CORE-011 d7)
# ---------------------------------------------------------------------------


def test_garage_declares_its_daemon_version_from_the_last_state() -> None:
    hook = GARAGE_INTEGRATION.components
    assert hook is not None
    assert hook(None, None) == ()  # nothing before the first read
    assert hook(None, make_fake_garage_state()) == (SdkComponent("garage", "v", ""),)


# ---------------------------------------------------------------------------
# The push envelope (CORE-011 d5): generation, complete, changed_ids, the ack
# ---------------------------------------------------------------------------


def _fresh(*ids: str, complete: bool = True, at: str = "t0") -> GarageState:
    return replace(make_fake_garage_state(), read=Freshness(complete, at, ids))


def test_replace_runtime_state_folds_freshness_and_accumulates_changed_ids() -> None:
    rt = _runtime(None)
    replace_runtime_state(rt, _fresh(_MERGE_ID_A, complete=False))
    assert (rt.complete, rt.last_full_read_at) == (False, "t0")
    replace_runtime_state(rt, _fresh(_MERGE_ID_B, _MERGE_ID_A, at="t1"))
    # Until an ack, every id read since the last one is owed, each once.
    assert rt.changed_ids == [_MERGE_ID_A, _MERGE_ID_B]
    assert (rt.complete, rt.last_full_read_at) == (True, "t1")


def test_replace_runtime_state_with_a_blob_that_says_nothing_reads_as_complete() -> (
    None
):
    rt = _runtime(None)
    rt.complete = False
    replace_runtime_state(rt, _UnmergeableState())
    assert rt.state is not None
    assert (rt.complete, rt.changed_ids) == (False, [])


def test_merge_into_runtime_owes_the_merged_ids() -> None:
    rt = _runtime(make_fake_garage_state())
    merge_items_into_runtime(
        rt, [make_garage_bucket(_MERGE_ID_A), make_garage_bucket("")]
    )
    assert rt.changed_ids == [_MERGE_ID_A]


@pytest.mark.asyncio
@patch("stormpulse.agent.integrations_runtime.collect_metrics")
async def test_push_advances_generation_and_the_ack_releases_only_what_it_carried(
    mock_collect: MagicMock, agent_with_garage: Callable[..., Agent]
) -> None:
    mock_collect.return_value = FAKE_METRICS
    ag = agent_with_garage()
    rt = ag.integrations["garage"]
    replace_runtime_state(rt, _fresh(_MERGE_ID_A))
    first_envelope = await build_metrics_envelope(ag)
    first = first_envelope.payload["integrations"]["garage"]
    assert (first["generation"], first["changed_ids"]) == (1, [_MERGE_ID_A])
    assert (first["complete"], first["last_full_read_at"]) == (True, "t0")
    # No ack: the next push repeats the id, and adds what was read since.
    replace_runtime_state(rt, _fresh(_MERGE_ID_B))
    second_envelope = await build_metrics_envelope(ag)
    second = second_envelope.payload["integrations"]["garage"]
    assert (second["generation"], second["changed_ids"]) == (
        2,
        [_MERGE_ID_A, _MERGE_ID_B],
    )
    # Read between the push and its ack: owed to the next one, not released.
    merge_items_into_runtime(rt, [make_garage_bucket("c" * 64)])
    acknowledge_push(ag.integrations, second_envelope.id)
    assert rt.changed_ids == ["c" * 64]
    assert (await build_metrics_envelope(ag)).payload["integrations"]["garage"][
        "generation"
    ] == 3


def test_an_ack_with_no_push_behind_it_releases_nothing() -> None:
    rt = _runtime(None)
    replace_runtime_state(rt, _fresh(_MERGE_ID_A))
    acknowledge_push({"garage": rt}, "unknown")
    assert rt.changed_ids == [_MERGE_ID_A]
