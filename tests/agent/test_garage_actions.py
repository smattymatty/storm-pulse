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
from tests.helpers import FAKE_METRICS, make_fake_garage_state

# ---------------------------------------------------------------------------
# build_metrics_envelope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("stormpulse.agent.garage_actions.collect_metrics")
async def test_build_metrics_envelope_without_garage_state(
    mock_collect: MagicMock,
    agent: Agent,
) -> None:
    """When no garage state is set, the envelope has ``garage=None``."""
    mock_collect.return_value = FAKE_METRICS
    envelope = await build_metrics_envelope(agent)
    assert envelope.payload["garage"] is None
    assert envelope.payload["cpu_percent"] == FAKE_METRICS.cpu_percent


@pytest.mark.asyncio
@patch("stormpulse.agent.garage_actions.collect_metrics")
async def test_build_metrics_envelope_includes_garage_snapshot(
    mock_collect: MagicMock,
    agent: Agent,
) -> None:
    """When garage state is present it rides as a dict on the envelope."""
    mock_collect.return_value = FAKE_METRICS
    agent.garage_state = make_fake_garage_state()
    envelope = await build_metrics_envelope(agent)
    assert envelope.payload["garage"] is not None
    assert envelope.payload["garage"]["node_id"] == "n1"


# ---------------------------------------------------------------------------
# refresh_garage_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_garage_state_noop_without_garage_config(
    agent: Agent,
) -> None:
    """An agent with no [garage] config keeps ``_garage_state = None``."""
    with patch("stormpulse.agent.garage_actions.collect_garage_state") as mock_collect:
        await refresh_garage_state(agent)
        mock_collect.assert_not_called()
    assert agent.garage_state is None


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
    assert ag.garage_state is fake


@pytest.mark.asyncio
@patch("stormpulse.agent.garage_actions.collect_garage_state", return_value=None)
async def test_refresh_garage_state_leaves_state_untouched_on_collect_failure(
    _mock: MagicMock,
    agent_with_garage: Callable[..., Agent],
) -> None:
    """A None result from the collector must not overwrite an existing snapshot."""
    ag = agent_with_garage()
    prior = make_fake_garage_state()
    ag.garage_state = prior
    await refresh_garage_state(ag)
    assert ag.garage_state is prior


# ---------------------------------------------------------------------------
# post_success_hook
# ---------------------------------------------------------------------------


def test_post_success_hook_none_for_non_garage_group(agent: Agent) -> None:
    cmd_def = CommandDef(group="deploy", command=["/bin/git", "pull"], timeout=60)
    assert post_success_hook(agent, cmd_def, "git_pull") is None


def test_post_success_hook_none_when_garage_disabled(
    agent_with_garage: Callable[..., Agent],
) -> None:
    """A garage-group command on an agent with [garage].enabled=False gets no hook."""
    cmd_def = CommandDef(
        group="garage", command=["/garage"], timeout=60, long_running=True
    )
    ag = agent_with_garage(enabled=False)
    assert post_success_hook(ag, cmd_def, "garage_bucket_clear") is None


def test_post_success_hook_present_when_garage_enabled(
    agent_with_garage: Callable[..., Agent],
) -> None:
    cmd_def = CommandDef(
        group="garage", command=["/garage"], timeout=60, long_running=True
    )
    ag = agent_with_garage()
    assert post_success_hook(ag, cmd_def, "garage_bucket_clear") is not None


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

    assert ag.garage_state is fake_state
    assert len(sent) == 1
    assert sent[0].payload["garage"] is not None

    await ag.job_manager.shutdown_all()
