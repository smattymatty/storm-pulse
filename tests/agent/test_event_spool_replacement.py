"""A spool segment replaced under its name (the emitter renames a new file
over it) while it is pending, settling or being read: settling the old file
never deletes or ships the new one in its place."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from stormpulse import events
from stormpulse.agent import Agent
from stormpulse.agent.acks import handle_events_batch_nack
from stormpulse.agent.event_spool import MAX_SEGMENT_EVENTS, SpoolFeeder
from stormpulse.bounded_read import read_bounded
from stormpulse.events import MAX_IN_FLIGHT_BATCHES
from tests.agent.spool_support import NOBODY, nack, record_ids, seen, segment

pytestmark = pytest.mark.usefixtures("event_registry")
needs_root = pytest.mark.skipif(
    os.geteuid() != 0, reason="needs root to create a file another user owns"
)


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


def _replace_during_read(monkeypatch: pytest.MonkeyPatch, spool: Path) -> list[Path]:
    """Gate: the emitter renames a new ``0001.jsonl`` over the name as the
    first read returns or raises. Returns the replacement once it landed."""
    landed = list[Path]()

    def read_then_replace(*args: Any) -> Any:
        try:
            return read_bounded(*args)
        finally:
            if not landed:
                landed.append(segment(spool, "0001.jsonl", seen("replacement")))

    monkeypatch.setattr("stormpulse.agent.event_spool.read_bounded", read_then_replace)
    return landed


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
    await handle_events_batch_nack(agent, AsyncMock(), nack("b1"))
    feeder.tick()
    assert seg.exists(), "the nack deleted the replacement"
    assert record_ids(events.buffer().drain("b2")) == ["replacement"]


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
    assert record_ids(buf.drain("b2")) == ["replacement"]


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
    assert record_ids(buf.drain("b2")) == ["successor"]


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
    assert record_ids(buf.drain("b2")) == [], "a parked segment shipped twice"


def test_a_segment_stranded_aside_by_a_crash_goes_back_on_restart(
    feeder: SpoolFeeder, spool: Path
) -> None:
    segment(spool, ".settling.0001.jsonl", seen("r1"))
    restarted = SpoolFeeder(feeder.source, os.lstat(spool))
    restarted.tick()
    assert record_ids(events.buffer().drain("b1")) == ["r1"]
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
    assert record_ids(buf.drain("b2")) == []


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
    assert seg.exists() and record_ids(buf.drain("b3")) == [], "settled at one batch"
    aside = _emit_during_settle(monkeypatch, spool, seen("newest"))
    buf.ack("b2")
    feeder.tick()
    assert aside == [".settling.0001.jsonl"]
    assert record_ids(buf.drain("b4")) == ["newest"]
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
    assert record_ids(buf.drain("b3")) == ["replacement"]
    feeder.tick()
    assert seg.exists(), "the old batches settled the replacement's events"
    buf.ack("b3")
    feeder.tick()
    assert not seg.exists()


def test_a_replacement_landing_as_the_segment_is_read_is_not_mistaken_for_it(
    feeder: SpoolFeeder, spool: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file read is the one its own fstat names, never a later lstat."""
    landed = _replace_during_read(monkeypatch, spool)
    buf = events.buffer()
    seg = segment(spool, "0001.jsonl", seen("old"))
    feeder.tick()
    assert landed and record_ids(buf.drain("b1")) == ["old"]
    buf.ack("b1")
    feeder.tick()
    assert seg.exists(), "the replacement was settled as the file read"
    assert record_ids(buf.drain("b2")) == ["replacement"]


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
    assert record_ids(events.buffer().drain("b1")) == ["stranded", "live"]


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
        lambda s: (s / "0001.jsonl").symlink_to(s.parent / "elsewhere.jsonl"),
    ],
    ids=["too-large", "not-regular", "foreign-owner", "symlink"],
)
def test_a_replacement_landing_as_a_refused_segment_is_read_survives_it(
    feeder: SpoolFeeder,
    spool: Path,
    monkeypatch: pytest.MonkeyPatch,
    refused: Callable[[Path], None],
) -> None:
    """A refused file settles at once, by the file it was refused on."""
    landed = _replace_during_read(monkeypatch, spool)
    refused(spool)
    feeder.tick()
    assert landed and landed[0].exists(), (
        "settling the refused file deleted its replacement"
    )
    feeder.tick()
    assert record_ids(events.buffer().drain("b1")) == ["replacement"]


def test_a_refused_file_renamed_in_before_the_open_still_settles(
    feeder: SpoolFeeder, spool: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An opened file is settled by what was opened, not by what the name held
    a moment before, or a refused replacement is refused again every tick."""
    segment(spool, "0001.jsonl", seen("old"))
    swapped = list[bool]()

    def replace_then_read(*args: Any) -> Any:
        if not swapped:
            swapped.append(True)
            _oversize(spool)
        return read_bounded(*args)

    monkeypatch.setattr("stormpulse.agent.event_spool.read_bounded", replace_then_read)
    feeder.tick()
    assert swapped
    assert not (spool / "0001.jsonl").exists(), "the refused file was never settled"


def test_a_directory_at_a_segment_name_is_parked_where_it_stands(
    feeder: SpoolFeeder, spool: Path
) -> None:
    """A directory cannot be linked back once moved aside, so it is never moved:
    the name stays usable, and the segment written there after it settles."""
    buf = events.buffer()
    (spool / "0001.jsonl").mkdir()
    feeder.tick()
    assert (spool / "0001.jsonl").is_dir(), "the directory was moved off its name"
    assert os.listdir(spool) == ["0001.jsonl"], "the directory took the aside slot"
    feeder.tick()
    (synthesized,) = buf.drain("b1")
    assert synthesized["undeclared"] == 1, "the parked directory was refused again"
    (spool / "0001.jsonl").rmdir()
    seg = segment(spool, "0001.jsonl", seen("r1"))
    feeder.tick()
    assert record_ids(buf.drain("b2")) == ["r1"]
    buf.ack("b2")
    feeder.tick()
    assert not seg.exists(), "the segment after the directory was never deleted"
    assert os.listdir(spool) == []


def test_a_directory_replacing_a_pending_segment_stays_at_its_name(
    feeder: SpoolFeeder, spool: Path
) -> None:
    buf = events.buffer()
    seg = segment(spool, "0001.jsonl", seen("old"))
    feeder.tick()
    assert record_ids(buf.drain("b1")) == ["old"]
    seg.unlink()
    seg.mkdir()  # the emitter puts a directory where the segment was
    buf.ack("b1")
    feeder.tick()
    assert seg.is_dir(), "settling moved the directory off its name"
    assert os.listdir(spool) == ["0001.jsonl"], "the directory took the aside slot"


def test_a_symlink_to_a_directory_settles_like_any_symlink(
    feeder: SpoolFeeder, spool: Path
) -> None:
    """Kind is read without following the link: only a real directory parks."""
    (spool.parent / "elsewhere").mkdir()
    (spool / "0001.jsonl").symlink_to(spool.parent / "elsewhere")
    feeder.tick()
    assert os.listdir(spool) == [], "the symlink was parked instead of settled"
    buf = events.buffer()
    buf.drain("b1")
    seg = segment(spool, "0001.jsonl", seen("r1"))
    feeder.tick()
    assert record_ids(buf.drain("b2")) == ["r1"]
    buf.ack("b2")
    feeder.tick()
    assert not seg.exists(), "the segment after the symlink was never deleted"


def test_a_name_whose_kind_cannot_be_read_still_settles(
    feeder: SpoolFeeder, spool: Path
) -> None:
    """The directory check is no exit: the emitter's spool bound deleted the
    acked segment, so lstat fails and settling falls through to the rename."""
    buf = events.buffer()
    seg = segment(spool, "0001.jsonl", seen("r1"))
    feeder.tick()
    buf.drain("b1")
    buf.ack("b1")
    seg.unlink()
    feeder.tick()
    assert os.listdir(spool) == [], "settling a gone name left an aside"
