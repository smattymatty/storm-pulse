"""Unit tests for ``stormpulse.agent.garage_actions``.

The dispatcher-level path is covered by ``test_garage_refresh.py``;
these focus on the building blocks in isolation: the metrics-envelope
builder, the state-refresh helper, and the post-success hook factory.
"""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock, patch

import pytest

from stormpulse.agent import Agent
from stormpulse.agent.garage_actions import (
    build_metrics_envelope,
    merge_buckets_into_runtime,
    post_success_hook,
    refresh_affected_buckets,
)
from stormpulse.agent.integrations_runtime import IntegrationRuntime
from stormpulse.commands.jobs import JobManager
from stormpulse.config import CommandSpec
from stormpulse.garage.integration import GARAGE_INTEGRATION
from stormpulse.garage.state import (
    MAX_TARGETED_BUCKET_READS,
    GarageBucket,
    GarageKeyRef,
    GarageState,
)
from stormpulse.protocol import Envelope
from tests.helpers import (
    FAKE_METRICS,
    get_garage_state,
    make_fake_garage_state,
    make_garage_bucket,
    set_garage_state,
)

# ---------------------------------------------------------------------------
# build_metrics_envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("stormpulse.agent.garage_actions.collect_metrics")
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
@patch("stormpulse.agent.garage_actions.collect_metrics")
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


_ID_A = "a" * 64
_KEY = "GKkey"


def _owned_by(bucket_id: str, key_id: str) -> GarageBucket:
    return make_garage_bucket(bucket_id, keys=[GarageKeyRef(key_id, "k", "RWO")])


def _state_with(*buckets: GarageBucket) -> GarageState:
    return make_fake_garage_state().with_buckets(buckets)


# ---------------------------------------------------------------------------
# refresh_affected_buckets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("stormpulse.agent.garage_actions.read_buckets_by_id")
async def test_refresh_affected_merges_targeted_read(
    mock_read: MagicMock,
    agent_with_garage: Callable[..., Agent],
) -> None:
    """The named bucket is re-read and upserted; returns True."""
    ag = agent_with_garage()
    set_garage_state(ag, _state_with(make_garage_bucket(_ID_A, size_bytes=0)))
    mock_read.return_value = [make_garage_bucket(_ID_A, size_bytes=99)]

    assert await refresh_affected_buckets(ag, {"bucket_id": _ID_A}) is True
    state = get_garage_state(ag)
    assert isinstance(state, GarageState)
    assert state.buckets[0].size_bytes == 99


@pytest.mark.asyncio
@patch("stormpulse.agent.garage_actions.read_buckets_by_id")
async def test_refresh_affected_noops_when_nothing_resolved(
    mock_read: MagicMock,
    agent_with_garage: Callable[..., Agent],
) -> None:
    """No affected id (e.g. alias-only/new-bucket op): no admin read, no merge."""
    ag = agent_with_garage()
    prior = _state_with(make_garage_bucket(_ID_A))
    set_garage_state(ag, prior)
    assert await refresh_affected_buckets(ag, {}) is False
    mock_read.assert_not_called()
    assert get_garage_state(ag) is prior


@pytest.mark.asyncio
@patch("stormpulse.agent.garage_actions.read_buckets_by_id")
async def test_refresh_affected_false_without_baseline(
    mock_read: MagicMock,
    agent_with_garage: Callable[..., Agent],
) -> None:
    """A targeted merge needs a full snapshot to upsert into; cold start defers."""
    ag = agent_with_garage()
    set_garage_state(ag, None)
    assert await refresh_affected_buckets(ag, {"bucket_id": _ID_A}) is False
    mock_read.assert_not_called()


@pytest.mark.asyncio
@patch("stormpulse.agent.garage_actions.read_buckets_by_id")
async def test_refresh_affected_caps_key_fanout(
    mock_read: MagicMock,
    agent_with_garage: Callable[..., Agent],
) -> None:
    """A key owning more buckets than the cap re-reads at most MAX this pass."""
    ag = agent_with_garage()
    many = [_owned_by(f"{i:064x}", _KEY) for i in range(MAX_TARGETED_BUCKET_READS + 3)]
    set_garage_state(ag, _state_with(*many))
    mock_read.return_value = [make_garage_bucket(_ID_A)]  # truthy so merge runs

    await refresh_affected_buckets(ag, {"key_id": _KEY})
    passed_ids = mock_read.call_args[0][1]
    assert len(passed_ids) == MAX_TARGETED_BUCKET_READS


# ---------------------------------------------------------------------------
# merge_buckets_into_runtime (the rt.state race discipline)
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
    assert merge_buckets_into_runtime(rt, [make_garage_bucket(_MERGE_ID_A)]) is False
    assert rt.state is None


def test_merge_into_runtime_upserts_and_reassigns() -> None:
    prior = make_fake_garage_state()  # buckets=[]
    rt = _runtime(prior)
    ok = merge_buckets_into_runtime(rt, [make_garage_bucket(_MERGE_ID_A, size_bytes=7)])
    assert ok is True
    # rt.state is a NEW object (frozen merge), not the prior one mutated.
    assert rt.state is not prior
    assert [b.id for b in rt.state.buckets] == [_MERGE_ID_A]
    assert rt.state.buckets[0].size_bytes == 7


def test_merge_into_runtime_preserves_full_manifest() -> None:
    """Merging a newcomer keeps every already-known bucket (manifest, not partial)."""
    base = make_fake_garage_state()
    rt = _runtime(base.with_buckets([make_garage_bucket(_MERGE_ID_A)]))
    merge_buckets_into_runtime(rt, [make_garage_bucket(_MERGE_ID_B)])
    assert {b.id for b in rt.state.buckets} == {_MERGE_ID_A, _MERGE_ID_B}


# ---------------------------------------------------------------------------
# post_success_hook
# ---------------------------------------------------------------------------


def _garage_cmd(
    *, read_only: bool = False, self_reconciling: bool = False
) -> CommandSpec:
    return CommandSpec(
        group="garage", command=["/garage"], timeout=60, mode="job",
        handler=lambda _p: None,
        read_only=read_only, self_reconciling=self_reconciling,
    )


def test_post_success_hook_none_for_non_garage_group(agent: Agent) -> None:
    cmd_def = CommandSpec(group="deploy", command=["/bin/git", "pull"], timeout=60)
    assert post_success_hook(agent, cmd_def, "git_pull", {}) is None


def test_post_success_hook_present_for_mutation(
    agent_with_garage: Callable[..., Agent],
) -> None:
    ag = agent_with_garage()
    assert post_success_hook(ag, _garage_cmd(), "garage_bucket_clear", {}) is not None


def test_post_success_hook_none_for_self_reconciling(
    agent_with_garage: Callable[..., Agent],
) -> None:
    # A self_reconciling command (set_quota, converge) is dispatched repeatedly by
    # a reconciliation loop, so no single success warrants a push; the hook honors
    # the flag, never a hardcoded command name. (Which specs carry the flag is
    # tested in tests/garage against the real registry.)
    ag = agent_with_garage()
    assert post_success_hook(
        ag, _garage_cmd(self_reconciling=True), "garage_bucket_set_quota", {}
    ) is None


def test_post_success_hook_none_for_read_only(
    agent_with_garage: Callable[..., Agent],
) -> None:
    # Read-only garage long-runners (get_bucket_owners, get_key_buckets,
    # walk_bucket_stats) mutate nothing; the hook must skip them or a polling
    # dashboard floods state pushes. A mutating garage long-runner still gets one.
    ag = agent_with_garage()
    assert post_success_hook(
        ag, _garage_cmd(read_only=True), "garage_get_bucket_owners", {}
    ) is None
    assert post_success_hook(ag, _garage_cmd(), "garage_bucket_clear", {}) is not None


@pytest.mark.asyncio
@patch("stormpulse.agent.garage_actions.collect_metrics")
@patch("stormpulse.agent.garage_actions.read_buckets_by_id")
async def test_post_success_hook_refreshes_and_pushes(
    mock_read: MagicMock,
    mock_metrics: MagicMock,
    agent_with_garage: Callable[..., Agent],
) -> None:
    """The hook targeted-re-reads the named bucket and emits one metrics.push."""
    mock_read.return_value = [make_garage_bucket(_ID_A, size_bytes=42)]
    mock_metrics.return_value = FAKE_METRICS

    ag = agent_with_garage()
    set_garage_state(ag, _state_with(make_garage_bucket(_ID_A, size_bytes=0)))
    sent: list[Envelope] = []

    async def fake_send(env: Envelope) -> None:
        sent.append(env)

    ag.job_manager = JobManager(ag.config.agent.id, fake_send)

    hook = post_success_hook(ag, _garage_cmd(), "garage_bucket_clear", {"bucket_id": _ID_A})
    assert hook is not None
    await hook()

    state = get_garage_state(ag)
    assert isinstance(state, GarageState)
    assert state.buckets[0].size_bytes == 42
    assert len(sent) == 1
    assert sent[0].payload["integrations"]["garage"]["state"]["node_id"] == "n1"

    await ag.job_manager.shutdown_all()


@pytest.mark.asyncio
@patch("stormpulse.agent.garage_actions.collect_metrics")
@patch("stormpulse.agent.garage_actions.read_buckets_by_id")
async def test_post_success_hook_skips_push_when_nothing_affected(
    mock_read: MagicMock,
    mock_metrics: MagicMock,
    agent_with_garage: Callable[..., Agent],
) -> None:
    """A new-bucket/alias-only op resolves to nothing: no targeted read, no push."""
    mock_metrics.return_value = FAKE_METRICS
    ag = agent_with_garage()
    set_garage_state(ag, _state_with(make_garage_bucket(_ID_A)))
    sent: list[Envelope] = []

    async def fake_send(env: Envelope) -> None:
        sent.append(env)

    ag.job_manager = JobManager(ag.config.agent.id, fake_send)

    hook = post_success_hook(ag, _garage_cmd(), "garage_bucket_create", {"bucket_name": "new"})
    assert hook is not None
    await hook()

    mock_read.assert_not_called()
    assert sent == []

    await ag.job_manager.shutdown_all()
