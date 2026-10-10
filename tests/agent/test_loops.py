"""Tests for the periodic loop bodies."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from stormpulse import events
from stormpulse.agent import Agent, loops
from stormpulse.config import LogGroupConfig
from stormpulse.garage.state_reader import GarageStateReader
from stormpulse.integrations import Freshness
from stormpulse.logging.positions import LogPositionStore
from stormpulse.logging.shipper import LogShipper
from stormpulse.logging.tailer import LogTailer
from stormpulse.protocol import MetricsPayload
from tests.helpers import (
    FAKE_METRICS,
    get_garage_state,
    make_fake_garage_state,
)

# ---------------------------------------------------------------------------
# Heartbeat loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_loop_sends_messages(
    agent: Agent, shutdown: asyncio.Event
) -> None:
    ws = AsyncMock()
    sent: list[str] = []
    ws.send = AsyncMock(side_effect=lambda msg: sent.append(msg))

    async def stop_after_delay() -> None:
        await asyncio.sleep(0.15)
        shutdown.set()

    await asyncio.gather(
        loops.heartbeat_loop(agent, ws),
        stop_after_delay(),
    )
    assert len(sent) >= 2
    for msg in sent:
        data = json.loads(msg)
        assert data["type"] == "heartbeat"


@pytest.mark.asyncio
async def test_heartbeat_loop_stops_on_shutdown(
    agent: Agent, shutdown: asyncio.Event
) -> None:
    ws = AsyncMock()
    shutdown.set()
    await loops.heartbeat_loop(agent, ws)
    ws.send.assert_not_called()


# ---------------------------------------------------------------------------
# Metrics loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch("stormpulse.agent.integrations_runtime.collect_metrics")
async def test_metrics_loop_sends_metrics(
    mock_collect: MagicMock,
    agent: Agent,
    shutdown: asyncio.Event,
) -> None:
    mock_collect.return_value = FAKE_METRICS
    ws = AsyncMock()
    sent: list[str] = []
    ws.send = AsyncMock(side_effect=lambda msg: sent.append(msg))

    async def stop_after_delay() -> None:
        await asyncio.sleep(0.15)
        shutdown.set()

    await asyncio.gather(
        loops.metrics_loop(agent, ws),
        stop_after_delay(),
    )
    assert len(sent) >= 2
    for msg in sent:
        data = json.loads(msg)
        assert data["type"] == "metrics.push"
    assert mock_collect.call_count >= 2


@pytest.mark.asyncio
@patch("stormpulse.agent.integrations_runtime.collect_metrics")
async def test_metrics_loop_survives_collection_error(
    mock_collect: MagicMock,
    agent: Agent,
    shutdown: asyncio.Event,
) -> None:
    call_count = 0

    def side_effect(*args: object) -> MetricsPayload:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("psutil broke")
        return FAKE_METRICS

    mock_collect.side_effect = side_effect
    ws = AsyncMock()

    async def stop_after_delay() -> None:
        await asyncio.sleep(0.15)
        shutdown.set()

    await asyncio.gather(
        loops.metrics_loop(agent, ws),
        stop_after_delay(),
    )
    # Should have continued past the error and sent at least one metrics push
    assert ws.send.call_count >= 1


# ---------------------------------------------------------------------------
# Integration state loop (disabled case gated at reconnect - see TestGarageLiveGate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch.object(GarageStateReader, "collect")
async def test_integration_state_loop_updates_state(
    mock_collect: MagicMock,
    agent_with_garage: Callable[..., Agent],
    shutdown: asyncio.Event,
) -> None:
    fake = make_fake_garage_state()
    mock_collect.return_value = fake
    ag = agent_with_garage()
    ws = AsyncMock()

    async def stop_after_delay() -> None:
        await asyncio.sleep(0.12)
        shutdown.set()

    await asyncio.gather(
        loops.integration_state_loop(ag, ws, "garage"), stop_after_delay()
    )
    assert get_garage_state(ag) is fake
    assert mock_collect.call_count >= 1


@pytest.mark.asyncio
@patch.object(GarageStateReader, "collect")
async def test_integration_state_loop_folds_what_the_read_said_of_itself(
    mock_collect: MagicMock,
    agent_with_garage: Callable[..., Agent],
    shutdown: asyncio.Event,
) -> None:
    """CORE-011 d5: the periodic collect is the third writer of ``runtime.state``;
    it owes the ids it re-read and carries the read's completeness like the others."""
    fresh = replace(make_fake_garage_state(), read=Freshness(False, "t0", ("a" * 64,)))

    def collect_once(config: object) -> object:
        shutdown.set()  # one tick, then the loop's sleep returns at once
        return fresh

    mock_collect.side_effect = collect_once
    ag = agent_with_garage()
    await loops.integration_state_loop(ag, AsyncMock(), "garage")
    rt = ag.integrations["garage"]
    assert get_garage_state(ag) is fresh
    assert (rt.complete, rt.last_full_read_at, list(rt.changed_revisions)) == (
        False,
        "t0",
        ["a" * 64],
    )


@pytest.mark.asyncio
@patch("stormpulse.agent.integrations_runtime.collect_metrics")
async def test_generation_does_not_rewind_across_sessions(
    mock_collect: MagicMock,
    agent_with_garage: Callable[..., Agent],
    shutdown: asyncio.Event,
) -> None:
    """The metrics loop is per session; the runtime it reads is per process
    (CORE-011 d5), so a reconnect's first push continues the count."""
    mock_collect.return_value = FAKE_METRICS
    ag = agent_with_garage()
    generations: list[int] = []

    async def send(msg: str) -> None:
        generations.append(
            json.loads(msg)["payload"]["integrations"]["garage"]["generation"]
        )
        shutdown.set()  # one push per session

    for _ in range(2):
        shutdown.clear()
        await loops.metrics_loop(ag, AsyncMock(send=AsyncMock(side_effect=send)))
    assert generations == [1, 2]


# ---------------------------------------------------------------------------
# Events and log loops: the frame each one puts on the wire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_loop_ships_the_drained_batch_and_keeps_it_in_flight(
    agent: Agent, shutdown: asyncio.Event
) -> None:
    """One ``events.batch`` per drain, the events as buffered, under the id the
    buffer holds in flight until the ack that names it."""
    buf = events.buffer()
    buf.append({"ts": "t", "source": "test", "kind": "one"})
    buf.append({"ts": "t", "source": "test", "kind": "two"})
    ws = AsyncMock()
    sent: list[str] = []
    ws.send = AsyncMock(side_effect=lambda msg: sent.append(msg))

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        shutdown.set()

    await asyncio.gather(loops.events_loop(agent, ws), stop_soon())
    assert len(sent) == 1
    frame = json.loads(sent[0])
    assert frame["type"] == "events.batch"
    assert [e["kind"] for e in frame["payload"]["events"]] == ["one", "two"]
    assert len(buf) == 0
    assert buf.ack(frame["payload"]["batch_id"]) is True


@pytest.mark.asyncio
async def test_log_loop_ships_the_collected_batch_as_the_group_declares_it(
    agent: Agent, shutdown: asyncio.Event, tmp_path: Path
) -> None:
    """A ``log.batch`` names the group and its parser, carries the parsed lines
    and the positions, and is pending until the ack with its id arrives."""
    log = tmp_path / "pulse.log"
    log.write_text(
        json.dumps(
            {
                "ts": "2026-04-10T13:00:00Z",
                "level": "INFO",
                "message": "m",
                "event_type": "connection",
            }
        )
        + "\n"
    )
    group = LogGroupConfig(
        name="pulse",
        enabled=True,
        source_type="file",
        source_path=log,
        filter_contains="",
        parser="stormpulse",
        ship_interval_seconds=10.0,
        max_lines_per_batch=50,
    )
    store = LogPositionStore(tmp_path / "pos.db")
    agent.shippers["pulse"] = LogShipper(group, LogTailer(group, store))
    ws = AsyncMock()
    sent: list[str] = []
    ws.send = AsyncMock(side_effect=lambda msg: sent.append(msg))

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        shutdown.set()

    try:
        await asyncio.gather(
            loops.log_loop(agent, ws, "pulse", lambda: None), stop_soon()
        )
    finally:
        store.close()
    assert len(sent) == 1
    frame = json.loads(sent[0])
    assert frame["type"] == "log.batch"
    payload = frame["payload"]
    assert (payload["group"], payload["parser"], payload["dropped"]) == (
        "pulse",
        "stormpulse",
        0,
    )
    assert [line["message"] for line in payload["lines"]] == ["m"]
    assert payload["from_position"] == 0
    assert payload["to_position"] == log.stat().st_size
    assert payload["batch_id"] in agent.pending_batches
