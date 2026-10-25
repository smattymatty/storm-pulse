"""Shipping and settling a spool segment (CORE-011 d6): its events ride
``events.batch``, and the segment goes only once every event it carried is
acked, nacked or dropped; one that cannot go is parked, never shipped twice."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from stormpulse import events
from stormpulse.agent import Agent, loops
from stormpulse.agent.acks import handle_events_batch_ack, handle_events_batch_nack
from stormpulse.agent.event_spool import MAX_SEGMENT_EVENTS, SpoolFeeder
from stormpulse.bounded_read import read_bounded
from stormpulse.events import MAX_IN_FLIGHT_BATCHES
from stormpulse.protocol import Envelope, MessageType
from tests.agent.spool_support import NOBODY, SOURCE, seen, segment
from tests.helpers import envelope

pytestmark = pytest.mark.usefixtures("event_registry")
needs_root = pytest.mark.skipif(
    os.geteuid() != 0, reason="needs root to create a file another user owns"
)


def _ack(batch_id: str) -> Envelope:
    return envelope(MessageType.EVENTS_BATCH_ACK, {"batch_id": batch_id})


def _nack(batch_id: str) -> Envelope:
    payload = {"batch_id": batch_id, "reason": "shape refused"}
    return envelope(MessageType.EVENTS_BATCH_NACK, payload)


def _ids(batch: list[dict[str, Any]]) -> list[str]:
    return [e["record_id"] for e in batch if e["kind"] == "request_seen"]


def _emit_during_settle(
    monkeypatch: pytest.MonkeyPatch, spool: Path, *records: dict[str, Any]
) -> list[str]:
    """Gate: the emitter renames a new ``0001.jsonl`` in the instant settling
    has moved the old one aside. Returns the names moved aside, as proof."""
    real, aside = os.rename, list[str]()

    def rename(src: Any, dst: Any) -> None:
        real(src, dst)
        if Path(dst).name.startswith(".settling.") and not aside:
            aside.append(Path(dst).name)
            segment(spool, "0001.jsonl", *records)

    monkeypatch.setattr(os, "rename", rename)
    return aside


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
    await handle_events_batch_nack(agent, AsyncMock(), _nack("b1"))
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


def test_settling_a_replaced_segment_keeps_the_replacement(
    feeder: SpoolFeeder, spool: Path
) -> None:
    """STAB-03: the emitter renames a new segment over a name still pending;
    settling the old one must not delete the file that replaced it."""
    buf = events.buffer()
    seg = segment(spool, "0001.jsonl", seen("old"))
    feeder.tick()
    assert [e["record_id"] for e in buf.drain("b1")] == ["old"]
    segment(spool, "0001.jsonl", seen("replacement"))  # atomic rename over it
    buf.ack("b1")
    feeder.tick()
    assert seg.exists(), "settling the old segment deleted its replacement"
    assert [e["record_id"] for e in buf.drain("b2")] == ["replacement"]


@pytest.mark.asyncio
async def test_nacking_a_replaced_segment_keeps_the_replacement(
    feeder: SpoolFeeder, spool: Path, agent: Agent
) -> None:
    seg = segment(spool, "0001.jsonl", seen("old"))
    feeder.tick()
    events.buffer().drain("b1")
    segment(spool, "0001.jsonl", seen("replacement"))
    await handle_events_batch_nack(agent, AsyncMock(), _nack("b1"))
    feeder.tick()
    assert seg.exists(), "the nack deleted the replacement"
    assert _ids(events.buffer().drain("b2")) == ["replacement"]


def test_evicting_a_replaced_segment_keeps_the_replacement(
    feeder: SpoolFeeder, spool: Path
) -> None:
    buf = events.buffer()
    seg = segment(spool, "0001.jsonl", seen("old"))
    feeder.tick()
    buf.drain("b1")
    for n in range(MAX_IN_FLIGHT_BATCHES):  # a silent control plane evicts b1
        buf.append({"kind": "filler"})
        buf.drain(f"f{n}")
    segment(spool, "0001.jsonl", seen("replacement"))
    feeder.tick()
    assert seg.exists(), "the eviction deleted the replacement"
    assert _ids(buf.drain("b2")) == ["replacement"]


def test_a_read_segment_the_emitter_deleted_leaves_its_successor(
    feeder: SpoolFeeder, spool: Path
) -> None:
    """The emitter's spool bound deletes the read file, then reuses its name."""
    buf = events.buffer()
    seg = segment(spool, "0001.jsonl", seen("old"))
    feeder.tick()
    buf.drain("b1")
    seg.unlink()
    segment(spool, "0001.jsonl", seen("successor"))
    buf.ack("b1")
    feeder.tick()
    assert seg.exists(), "settling a missing file deleted its successor"
    assert _ids(buf.drain("b2")) == ["successor"]


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
    assert _ids(buf.drain("b2")) == ["replacement"], "the replacement was parked"


@pytest.mark.parametrize("refusal", ["once", "always"])
def test_a_segment_that_will_not_unlink_goes_back_under_its_name(
    feeder: SpoolFeeder,
    spool: Path,
    monkeypatch: pytest.MonkeyPatch,
    refusal: str,
) -> None:
    persistent = refusal == "always"
    buf = events.buffer()
    seg = segment(spool, "0001.jsonl", seen("r1"))
    feeder.tick()
    buf.drain("b1")
    buf.ack("b1")
    real, refused = os.unlink, list[Any]()

    def unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        if Path(path).name.startswith(".settling.") and (persistent or not refused):
            refused.append(path)
            raise PermissionError("immutable")
        real(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)
    feeder.tick()
    assert refused, "settling never tried to unlink the file moved aside"
    assert seg.exists(), "a file that would not unlink was left aside"
    if not persistent:
        assert sorted(os.listdir(spool)) == ["0001.jsonl"]
    feeder.tick()
    assert _ids(buf.drain("b2")) == [], "a parked segment shipped twice"


def test_a_segment_stranded_aside_by_a_crash_goes_back_on_restart(
    feeder: SpoolFeeder, spool: Path
) -> None:
    segment(spool, ".settling.0001.jsonl", seen("r1"))
    restarted = SpoolFeeder(feeder.source, os.lstat(spool))
    restarted.tick()
    assert _ids(events.buffer().drain("b1")) == ["r1"]
    assert sorted(os.listdir(spool)) == ["0001.jsonl"]


def test_a_leftover_aside_of_the_file_read_never_keeps_it_shipping(
    feeder: SpoolFeeder, spool: Path
) -> None:
    """An aside that is a second name for the file read makes renaming onto it
    a no-op; settling clears it first, so the file goes and ships once."""
    buf = events.buffer()
    seg = segment(spool, "0001.jsonl", seen("r1"))
    feeder.tick()
    buf.drain("b1")
    os.link(seg, spool / ".settling.0001.jsonl")
    buf.ack("b1")
    feeder.tick()
    assert not seg.exists(), "the file read survived its settling"
    assert os.listdir(spool) == []
    feeder.tick()
    assert _ids(buf.drain("b2")) == []


def test_a_replacement_during_a_split_segment_is_never_settled_by_its_batches(
    feeder: SpoolFeeder, spool: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The name is replaced while the old file spans two batches, and again
    while settling has the first replacement aside: the newest file wins."""
    buf = events.buffer()
    buf.append({"kind": "filler"})
    old = (seen(str(n)) for n in range(MAX_SEGMENT_EVENTS))
    seg = segment(spool, "0001.jsonl", *old)
    feeder.tick()
    buf.drain("b1"), buf.drain("b2")
    segment(spool, "0001.jsonl", seen("first"))
    buf.ack("b1")
    feeder.tick()
    assert seg.exists() and _ids(buf.drain("b3")) == [], "settled at one batch"
    aside = _emit_during_settle(monkeypatch, spool, seen("newest"))
    buf.ack("b2")
    feeder.tick()
    assert aside == [".settling.0001.jsonl"]
    assert _ids(buf.drain("b4")) == ["newest"]
    assert sorted(os.listdir(spool)) == ["0001.jsonl"]


def test_a_replacement_landing_once_settling_starts_keeps_its_own_events(
    feeder: SpoolFeeder, spool: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    buf = events.buffer()
    buf.append({"kind": "filler"})
    old = (seen(str(n)) for n in range(MAX_SEGMENT_EVENTS))
    seg = segment(spool, "0001.jsonl", *old)
    feeder.tick()
    buf.drain("b1"), buf.drain("b2")
    buf.ack("b1"), buf.ack("b2")
    aside = _emit_during_settle(monkeypatch, spool, seen("replacement"))
    feeder.tick()
    assert aside, "settling never moved the old file aside"
    assert _ids(buf.drain("b3")) == ["replacement"]
    feeder.tick()
    assert seg.exists(), "the old batches settled the replacement's events"
    buf.ack("b3")
    feeder.tick()
    assert not seg.exists()


def test_a_replacement_landing_as_the_segment_is_read_is_not_mistaken_for_it(
    feeder: SpoolFeeder, spool: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file read is the one its own fstat names, never a later lstat."""
    landed = list[Path]()

    def read_then_replace(*args: Any) -> Any:
        read = read_bounded(*args)
        if not landed:
            landed.append(segment(spool, "0001.jsonl", seen("replacement")))
        return read

    target = "stormpulse.agent.event_spool.read_bounded"
    monkeypatch.setattr(target, read_then_replace)
    buf = events.buffer()
    seg = segment(spool, "0001.jsonl", seen("old"))
    feeder.tick()
    assert landed and _ids(buf.drain("b1")) == ["old"]
    buf.ack("b1")
    feeder.tick()
    assert seg.exists(), "the replacement was settled as the file read"
    assert _ids(buf.drain("b2")) == ["replacement"]


def test_a_restart_returns_only_segment_asides_and_keeps_live_segments(
    feeder: SpoolFeeder, spool: Path
) -> None:
    """Recovery touches a stranded segment only: a live one and a foreign
    dot-file that merely shares the prefix stay where they are."""
    segment(spool, "0002.jsonl", seen("live"))
    segment(spool, ".settling.0001.jsonl", seen("stranded"))
    (spool / ".settling.notes").write_text("not a segment\n")
    restarted = SpoolFeeder(feeder.source, os.lstat(spool))
    listed = sorted(os.listdir(spool))
    assert listed == [".settling.notes", "0001.jsonl", "0002.jsonl"]
    restarted.tick()
    assert _ids(events.buffer().drain("b1")) == ["stranded", "live"]


def _oversize(spool: Path) -> None:
    segment(spool, "0001.jsonl", *(seen(f"{n:04}" + "r" * 3000) for n in range(400)))


def _foreign(spool: Path) -> None:
    os.chown(segment(spool, "0001.jsonl", seen("old")), NOBODY, NOBODY)


@pytest.mark.parametrize(
    "refused",
    [
        _oversize,
        lambda spool: os.mkfifo(spool / "0001.jsonl"),
        pytest.param(_foreign, marks=needs_root),
    ],
    ids=["too-large", "not-regular", "foreign-owner"],
)
def test_a_replacement_landing_as_a_refused_segment_is_read_survives_it(
    feeder: SpoolFeeder,
    spool: Path,
    monkeypatch: pytest.MonkeyPatch,
    refused: Callable[[Path], None],
) -> None:
    """A refused file settles at once, by the fstat it was refused on."""
    landed = list[Path]()

    def read_then_replace(*args: Any) -> Any:
        try:
            return read_bounded(*args)
        finally:
            if not landed:
                landed.append(segment(spool, "0001.jsonl", seen("replacement")))

    monkeypatch.setattr("stormpulse.agent.event_spool.read_bounded", read_then_replace)
    refused(spool)
    feeder.tick()
    assert landed and landed[0].exists(), (
        "settling the refused file deleted its replacement"
    )
    feeder.tick()
    assert _ids(events.buffer().drain("b1")) == ["replacement"]


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
    assert _ids(buf.drain("b2")) == [], "the parked file read shipped twice"
