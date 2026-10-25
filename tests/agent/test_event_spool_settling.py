"""Shipping and settling a spool segment (CORE-011 d6): its events ride
``events.batch``, and the segment goes only once every event it carried is
acked, nacked or dropped; one that cannot go is parked, never shipped twice."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from stormpulse import events
from stormpulse.agent import Agent, loops
from stormpulse.agent.acks import handle_events_batch_ack, handle_events_batch_nack
from stormpulse.agent.event_spool import MAX_SEGMENT_EVENTS, SpoolFeeder
from stormpulse.protocol import Envelope, MessageType
from tests.agent.spool_support import SOURCE, nack, record_ids, seen, segment
from tests.helpers import envelope

pytestmark = pytest.mark.usefixtures("event_registry")


def _ack(batch_id: str) -> Envelope:
    return envelope(MessageType.EVENTS_BATCH_ACK, {"batch_id": batch_id})


@pytest.mark.asyncio
async def test_a_spooled_event_ships_on_events_batch_and_its_segment_goes_on_ack(
    feeder: SpoolFeeder, spool: Path, agent: Agent, shutdown: asyncio.Event
) -> None:
    seg = segment(spool, "0001.jsonl", seen("r1", ts="2026-10-13T00:00:00Z"))
    await asyncio.to_thread(feeder.tick)
    ws = AsyncMock()
    sent: list[str] = []
    ws.send = AsyncMock(side_effect=sent.append)

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        shutdown.set()

    await asyncio.gather(loops.events_loop(agent, ws), stop_soon())
    (frame,) = (json.loads(m) for m in sent)
    assert frame["type"] == "events.batch"
    (event,) = frame["payload"]["events"]
    assert (event["source"], event["kind"], event["record_id"]) == (
        SOURCE,
        "request_seen",
        "r1",
    )
    assert event["ts"] == "2026-10-13T00:00:00Z" and event["seq"] >= 1
    feeder.tick()
    assert seg.exists(), "deleted before the ack"
    await handle_events_batch_ack(agent, ws, _ack(frame["payload"]["batch_id"]))
    feeder.tick()
    assert not seg.exists()


@pytest.mark.asyncio
async def test_a_nacked_segment_is_deleted_and_the_loss_counted(
    feeder: SpoolFeeder, spool: Path, agent: Agent
) -> None:
    seg = segment(spool, "0001.jsonl", seen("r1"), seen("r2"))
    feeder.tick()
    events.buffer().drain("b1")
    await handle_events_batch_nack(agent, AsyncMock(), nack("b1"))
    feeder.tick()
    assert not seg.exists()
    (nacked,) = events.buffer().drain("b2")
    assert (nacked["kind"], nacked["dropped"]) == ("events_nacked", 2)


def test_a_full_buffer_leaves_the_segment_unread(
    feeder: SpoolFeeder, spool: Path
) -> None:
    buf = events.buffer()
    for _ in range(buf.room() - MAX_SEGMENT_EVENTS + 1):
        buf.append({"kind": "filler"})
    seg = segment(spool, "0001.jsonl", seen("r1"))
    feeder.tick()
    assert seg.exists()
    assert all(e["kind"] == "filler" for e in buf.drain("b", max_events=10_000))
    feeder.tick()
    assert [e["record_id"] for e in buf.drain("b2")] == ["r1"]


def test_a_reconnect_requeue_keeps_the_segment(
    feeder: SpoolFeeder, spool: Path
) -> None:
    seg = segment(spool, "0001.jsonl", seen("r1"))
    feeder.tick()
    events.buffer().drain("b1")
    events.buffer().requeue_unacked()
    feeder.tick()
    assert seg.exists()
    assert [e["record_id"] for e in events.buffer().drain("b2")] == ["r1"]


@pytest.mark.asyncio
async def test_the_spool_loop_ticks_until_shutdown(
    feeder: SpoolFeeder, spool: Path, agent: Agent, shutdown: asyncio.Event
) -> None:
    segment(spool, "0001.jsonl", seen("r1"))

    async def stop_soon() -> None:
        await asyncio.sleep(0.05)
        shutdown.set()

    await asyncio.gather(loops.event_spool_loop(agent, feeder), stop_soon())
    assert [e["record_id"] for e in events.buffer().drain("b")] == ["r1"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root deletes from a 0500 directory")
def test_a_segment_that_cannot_be_deleted_ships_once(
    feeder: SpoolFeeder, spool: Path
) -> None:
    """A spool the agent cannot write to after boot parks the settled segment
    instead of re-reading it every tick."""
    segment(spool, "0001.jsonl", seen("r1"))
    spool.chmod(0o500)
    buf = events.buffer()
    shipped = []
    for n in range(3):
        feeder.tick()
        batch = [e for e in buf.drain(f"b{n}") if e["kind"] == "request_seen"]
        shipped += batch
        buf.ack(f"b{n}")
    assert [e["record_id"] for e in shipped] == ["r1"]


def test_a_segment_split_across_batches_goes_only_when_both_are_acked(
    feeder: SpoolFeeder, spool: Path
) -> None:
    buf = events.buffer()
    buf.append({"kind": "filler"})
    seg = segment(
        spool, "0001.jsonl", *(seen(str(n)) for n in range(MAX_SEGMENT_EVENTS))
    )
    feeder.tick()
    first, second = buf.drain("b1"), buf.drain("b2")
    assert (len(first), len(second)) == (MAX_SEGMENT_EVENTS, 1)
    buf.ack("b1")
    feeder.tick()
    assert seg.exists(), "deleted with its last event still in flight"
    buf.ack("b2")
    feeder.tick()
    assert not seg.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root deletes from a 0500 directory")
def test_a_parked_segment_rewritten_by_the_emitter_is_read_afresh(
    feeder: SpoolFeeder, spool: Path
) -> None:
    buf = events.buffer()
    segment(spool, "0001.jsonl", seen("r1"))
    feeder.tick()
    buf.drain("b1")
    buf.ack("b1")
    spool.chmod(0o500)
    feeder.tick()  # settled, cannot delete: parked
    spool.chmod(0o700)
    segment(spool, "0001.jsonl", seen("r2"))  # a new inode under the name
    feeder.tick()
    assert [e["record_id"] for e in buf.drain("b2")] == ["r2"]


def test_a_failed_deletion_parks_the_file_read_not_its_replacement(
    feeder: SpoolFeeder, spool: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    buf = events.buffer()
    segment(spool, "0001.jsonl", seen("old"))
    feeder.tick()
    buf.drain("b1")
    segment(spool, "0001.jsonl", seen("replacement"))
    buf.ack("b1")
    real = os.rename

    def refuse_aside(src: Any, dst: Any) -> None:
        if Path(dst).name.startswith(".settling."):
            raise PermissionError("read-only spool")
        real(src, dst)

    with monkeypatch.context() as m:
        m.setattr(os, "rename", refuse_aside)
        feeder.tick()  # cannot delete: parks what was read
    feeder.tick()
    assert record_ids(buf.drain("b2")) == ["replacement"], "the replacement was parked"


def test_an_aside_that_cannot_go_back_is_never_renamed_over(
    feeder: SpoolFeeder, spool: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stranded aside the name cannot take back stays whole, and the file
    read is parked behind it, shipped once."""
    buf = events.buffer()
    seg = segment(spool, "0001.jsonl", seen("old"))
    feeder.tick()
    buf.drain("b1")
    stranded = segment(spool, ".settling.0001.jsonl", seen("stranded"))
    buf.ack("b1")

    def refuse_link(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("no hard links on this mount")

    monkeypatch.setattr(os, "link", refuse_link)
    feeder.tick()
    feeder.tick()
    assert stranded.exists() and "stranded" in stranded.read_text(), (
        "settling renamed the file read over a stranded aside"
    )
    assert seg.exists()
    assert record_ids(buf.drain("b2")) == [], "the parked file read shipped twice"
