"""Unit tests for the generic post-success hook in ``stormpulse.agent.dispatch``.

The hook resolves a mutating command's owning Integration via ``cmd_def.group``
(group == id) and fires its ``read_affected`` capability: targeted re-read,
atomic merge, one metrics push. Garage is the reference implementer.
"""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock, patch

import pytest

from stormpulse.agent import Agent
from stormpulse.agent.dispatch import post_success_hook
from stormpulse.agent.integrations_runtime import IntegrationRuntime
from stormpulse.caddy.integration import CADDY_INTEGRATION
from stormpulse.commands.jobs import JobManager
from stormpulse.config import CommandSpec
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

_ID_A = "a" * 64
_KEY = "GKkey"


def _owned_by(bucket_id: str, key_id: str) -> GarageBucket:
    return make_garage_bucket(bucket_id, keys=[GarageKeyRef(key_id, "k", "RWO")])


def _state_with(*buckets: GarageBucket) -> GarageState:
    return make_fake_garage_state().with_items(buckets)


def _garage_cmd(
    *, read_only: bool = False, self_reconciling: bool = False
) -> CommandSpec:
    return CommandSpec(
        group="garage", command=["/garage"], timeout=60, mode="job",
        handler=lambda _p: None,
        read_only=read_only, self_reconciling=self_reconciling,
    )


# ---------------------------------------------------------------------------
# Build-time gates
# ---------------------------------------------------------------------------


def test_post_success_hook_none_for_non_integration_group(agent: Agent) -> None:
    """A group naming no Integration (built-ins, config commands) is never hooked."""
    cmd_def = CommandSpec(group="deploy", command=["/bin/git", "pull"], timeout=60)
    assert post_success_hook(agent, cmd_def, "git_pull", {}) is None


def test_post_success_hook_none_without_read_affected(agent: Agent) -> None:
    """An Integration that declares no read_affected (caddy) is never hooked."""
    agent.integrations["caddy"] = IntegrationRuntime(
        id="caddy", status="live", disabled_reason=None,
        config=None, descriptor=CADDY_INTEGRATION,
    )
    cmd_def = CommandSpec(
        group="caddy", command=["/caddy"], timeout=60,
        mode="job", handler=lambda _p: None,
    )
    assert post_success_hook(agent, cmd_def, "caddy_reload", {}) is None


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


# ---------------------------------------------------------------------------
# Fire-time behavior (garage's read_affected as the reference implementer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("stormpulse.agent.integrations_runtime.collect_metrics")
@patch("stormpulse.garage.state.read_buckets_by_id")
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
@patch("stormpulse.agent.integrations_runtime.collect_metrics")
@patch("stormpulse.garage.state.read_buckets_by_id")
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


@pytest.mark.asyncio
@patch("stormpulse.agent.integrations_runtime.collect_metrics")
@patch("stormpulse.garage.state.read_buckets_by_id")
async def test_post_success_hook_defers_without_baseline(
    mock_read: MagicMock,
    mock_metrics: MagicMock,
    agent_with_garage: Callable[..., Agent],
) -> None:
    """Cold start (no snapshot yet): no read, no push; the next full collect covers it."""
    mock_metrics.return_value = FAKE_METRICS
    ag = agent_with_garage()
    set_garage_state(ag, None)
    sent: list[Envelope] = []

    async def fake_send(env: Envelope) -> None:
        sent.append(env)

    ag.job_manager = JobManager(ag.config.agent.id, fake_send)

    hook = post_success_hook(ag, _garage_cmd(), "garage_bucket_clear", {"bucket_id": _ID_A})
    assert hook is not None
    await hook()

    mock_read.assert_not_called()
    assert sent == []

    await ag.job_manager.shutdown_all()


@pytest.mark.asyncio
@patch("stormpulse.agent.integrations_runtime.collect_metrics")
@patch("stormpulse.garage.state.read_buckets_by_id")
async def test_post_success_hook_caps_key_fanout(
    mock_read: MagicMock,
    mock_metrics: MagicMock,
    agent_with_garage: Callable[..., Agent],
) -> None:
    """A key owning more buckets than the cap re-reads at most MAX this pass."""
    mock_metrics.return_value = FAKE_METRICS
    ag = agent_with_garage()
    many = [_owned_by(f"{i:064x}", _KEY) for i in range(MAX_TARGETED_BUCKET_READS + 3)]
    set_garage_state(ag, _state_with(*many))
    mock_read.return_value = [make_garage_bucket(_ID_A)]  # truthy so merge runs

    async def fake_send(env: Envelope) -> None:
        return None

    ag.job_manager = JobManager(ag.config.agent.id, fake_send)

    hook = post_success_hook(ag, _garage_cmd(), "garage_key_deny", {"key_id": _KEY})
    assert hook is not None
    await hook()

    passed_ids = mock_read.call_args[0][1]
    assert len(passed_ids) == MAX_TARGETED_BUCKET_READS

    await ag.job_manager.shutdown_all()
