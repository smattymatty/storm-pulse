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
    post_success_hook,
    refresh_garage_state,
)
from stormpulse.commands.jobs import JobManager
from stormpulse.config import CommandDef
from stormpulse.protocol import Envelope
from tests.helpers import (
    FAKE_METRICS,
    get_garage_state,
    make_fake_garage_state,
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


# ---------------------------------------------------------------------------
# refresh_garage_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("stormpulse.agent.garage_actions.collect_garage_state")
async def test_refresh_garage_state_writes_to_agent(
    mock_collect: MagicMock,
    agent_with_garage: Callable[..., Agent],
) -> None:
    fake = make_fake_garage_state()
    mock_collect.return_value = fake
    ag = agent_with_garage()
    await refresh_garage_state(ag)
    assert get_garage_state(ag) is fake


@pytest.mark.asyncio
@patch("stormpulse.agent.garage_actions.collect_garage_state", return_value=None)
async def test_refresh_garage_state_leaves_state_untouched_on_collect_failure(
    _mock: MagicMock,
    agent_with_garage: Callable[..., Agent],
) -> None:
    """A None result from the collector must not overwrite an existing snapshot."""
    ag = agent_with_garage()
    prior = make_fake_garage_state()
    set_garage_state(ag, prior)
    await refresh_garage_state(ag)
    assert get_garage_state(ag) is prior


# ---------------------------------------------------------------------------
# post_success_hook
# ---------------------------------------------------------------------------


def test_post_success_hook_none_for_non_garage_group(agent: Agent) -> None:
    cmd_def = CommandDef(group="deploy", command=["/bin/git", "pull"], timeout=60)
    assert post_success_hook(agent, cmd_def, "git_pull") is None


def test_post_success_hook_present_when_garage_enabled(
    agent_with_garage: Callable[..., Agent],
) -> None:
    cmd_def = CommandDef(
        group="garage", command=["/garage"], timeout=60, long_running=True
    )
    ag = agent_with_garage()
    assert post_success_hook(ag, cmd_def, "garage_bucket_clear") is not None


def test_post_success_hook_none_for_set_quota(
    agent_with_garage: Callable[..., Agent],
) -> None:
    # BUCKETS-006 invariant 2: set_quota is the recompute's OWN action, so it must
    # not emit a post-mutation metrics push, which would re-trigger the recompute
    # (the feedback storm). Its hook is None even though it is a garage command.
    cmd_def = CommandDef(
        group="garage",
        command=["garage_bucket_set_quota"],
        timeout=30,
        long_running=True,
    )
    ag = agent_with_garage()
    assert post_success_hook(ag, cmd_def, "garage_bucket_set_quota") is None


@pytest.mark.asyncio
@patch("stormpulse.agent.garage_actions.collect_metrics")
@patch("stormpulse.agent.garage_actions.collect_garage_state")
async def test_post_success_hook_refreshes_and_pushes(
    mock_collect: MagicMock,
    mock_metrics: MagicMock,
    agent_with_garage: Callable[..., Agent],
) -> None:
    """Invoking the hook refreshes state and emits a metrics.push via JobManager."""
    fake_state = make_fake_garage_state()
    mock_collect.return_value = fake_state
    mock_metrics.return_value = FAKE_METRICS

    ag = agent_with_garage()
    sent: list[Envelope] = []

    async def fake_send(env: Envelope) -> None:
        sent.append(env)

    ag.job_manager = JobManager(ag.config.agent.id, fake_send)

    cmd_def = CommandDef(
        group="garage", command=["/garage"], timeout=60, long_running=True
    )
    hook = post_success_hook(ag, cmd_def, "garage_bucket_clear")
    assert hook is not None
    await hook()

    assert get_garage_state(ag) is fake_state
    assert len(sent) == 1
    assert sent[0].payload["integrations"]["garage"]["state"]["node_id"] == "n1"

    await ag.job_manager.shutdown_all()
