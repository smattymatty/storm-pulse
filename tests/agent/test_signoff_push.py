"""Tests for the signoff.state push loop.

The loop pushes a single ``signoff.state`` envelope on every observed
seal transition. The initial state is already advertised by
``register``, so the loop seeds ``last_sealed`` from the agent's
current state and emits only on subsequent transitions. See ADR
CORE-004 "Live propagation" and the spec's `signoff.state` section.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock

import pytest

from stormpulse.agent import Agent, signoff_push


@pytest.fixture
def fast_push_interval() -> Generator[None, None, None]:
    """Shrink the poll interval so tests can observe transitions in <1s."""
    original = signoff_push.POLL_INTERVAL_SECONDS
    signoff_push.POLL_INTERVAL_SECONDS = 0.02
    try:
        yield
    finally:
        signoff_push.POLL_INTERVAL_SECONDS = original


def _sent_signoff_states(ws: AsyncMock) -> list[dict[str, Any]]:
    """Return every signoff.state envelope sent through *ws*, oldest first."""
    out: list[dict[str, Any]] = []
    for call in ws.send.await_args_list:
        raw = call.args[0]
        msg = json.loads(raw)
        if msg.get("type") == "signoff.state":
            out.append(msg)
    return out


@pytest.mark.asyncio
async def test_no_emit_when_state_unchanged(
    agent: Agent,
    shutdown: asyncio.Event,
    fast_push_interval: None,
) -> None:
    """A sealed agent that stays sealed emits no signoff.state."""
    agent.signoff_state.seal()
    ws = AsyncMock()

    async def stop_after() -> None:
        await asyncio.sleep(0.1)
        shutdown.set()

    await asyncio.gather(
        signoff_push.signoff_state_push_loop(agent, ws),
        stop_after(),
    )
    assert _sent_signoff_states(ws) == []


@pytest.mark.asyncio
async def test_emits_on_sealed_to_unsealed_transition(
    agent: Agent,
    shutdown: asyncio.Event,
    fast_push_interval: None,
) -> None:
    """A flip from sealed to unsealed pushes exactly one signoff.state."""
    agent.signoff_state.seal()
    ws = AsyncMock()

    async def flip_then_stop() -> None:
        await asyncio.sleep(0.05)
        agent.signoff_state.unseal()
        await asyncio.sleep(0.1)
        shutdown.set()

    await asyncio.gather(
        signoff_push.signoff_state_push_loop(agent, ws),
        flip_then_stop(),
    )
    sent = _sent_signoff_states(ws)
    assert len(sent) == 1
    payload = sent[0]["payload"]
    assert payload["signoff_sealed"] is False
    assert isinstance(payload["unsealed_since"], str)
    assert payload["unsealed_since"]


@pytest.mark.asyncio
async def test_emits_on_unsealed_to_sealed_transition(
    agent: Agent,
    shutdown: asyncio.Event,
    fast_push_interval: None,
) -> None:
    """A reseal pushes signoff_sealed=True with unsealed_since cleared."""
    agent.signoff_state.seal()
    agent.signoff_state.unseal()
    ws = AsyncMock()

    async def flip_then_stop() -> None:
        await asyncio.sleep(0.05)
        agent.signoff_state.seal()
        await asyncio.sleep(0.1)
        shutdown.set()

    await asyncio.gather(
        signoff_push.signoff_state_push_loop(agent, ws),
        flip_then_stop(),
    )
    sent = _sent_signoff_states(ws)
    assert len(sent) == 1
    payload = sent[0]["payload"]
    assert payload["signoff_sealed"] is True
    assert payload["unsealed_since"] is None


@pytest.mark.asyncio
async def test_double_flip_emits_two_envelopes(
    agent: Agent,
    shutdown: asyncio.Event,
    fast_push_interval: None,
) -> None:
    """Each transition emits its own envelope; no batching across ticks."""
    agent.signoff_state.seal()
    ws = AsyncMock()

    async def flip_then_stop() -> None:
        await asyncio.sleep(0.05)
        agent.signoff_state.unseal()
        await asyncio.sleep(0.1)
        agent.signoff_state.seal()
        await asyncio.sleep(0.1)
        shutdown.set()

    await asyncio.gather(
        signoff_push.signoff_state_push_loop(agent, ws),
        flip_then_stop(),
    )
    sent = _sent_signoff_states(ws)
    assert len(sent) == 2
    assert sent[0]["payload"]["signoff_sealed"] is False
    assert sent[1]["payload"]["signoff_sealed"] is True


@pytest.mark.asyncio
async def test_exits_promptly_on_shutdown(
    agent: Agent,
    shutdown: asyncio.Event,
) -> None:
    """Shutdown fired before the first wait should still terminate the loop."""
    agent.signoff_state.seal()
    shutdown.set()
    ws = AsyncMock()
    await asyncio.wait_for(
        signoff_push.signoff_state_push_loop(agent, ws),
        timeout=1.0,
    )
