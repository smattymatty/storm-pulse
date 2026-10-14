"""Reading a spool segment (CORE-011 d6): each event refused on its own for an
undeclared field or bad value, the whole segment refused and counted for an
unknown schema, an oversize or foreign-owned file, or one over the event cap."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from stormpulse import events
from stormpulse.agent.event_spool import (
    MAX_RECORD_BYTES,
    MAX_SEGMENT_BYTES,
    MAX_SEGMENT_EVENTS,
    SpoolFeeder,
)
from tests.agent.spool_support import NOBODY, seen, segment

pytestmark = pytest.mark.usefixtures("event_registry")
needs_root = pytest.mark.skipif(
    os.geteuid() != 0, reason="needs root to create a file another user owns"
)


def test_an_undeclared_field_refuses_the_event_and_counts_it(
    feeder: SpoolFeeder, spool: Path
) -> None:
    segment(
        spool,
        "0001.jsonl",
        seen("r1"),
        seen("r2", client_ip="10.0.0.1"),
        seen("r3", source="jobs"),
        seen("r4", status={"nested": 1}),
        {"kind": "unknown_kind", "record_id": "r5"},
        seen("r6", ts="yesterday"),
        seen("r7", duration_ms=float("nan")),
    )
    feeder.tick()
    synthesized, *shipped = events.buffer().drain("b")
    assert synthesized["kind"] == "dropped_events"
    assert synthesized["undeclared"] == 6
    assert [e["record_id"] for e in shipped] == ["r1"]
    assert "client_ip" not in json.dumps(shipped)


def test_an_unknown_schema_is_refused_whole_and_deleted(
    feeder: SpoolFeeder, spool: Path
) -> None:
    seg = segment(spool, "0001.jsonl", seen("r1"), seen("r2"), schema=2)
    feeder.tick()
    (synthesized,) = events.buffer().drain("b")
    assert synthesized["undeclared"] == 2
    assert not seg.exists()


def test_temp_files_and_symlinks_are_never_read(
    feeder: SpoolFeeder, spool: Path
) -> None:
    (spool / ".0001.jsonl.tmp").write_text('{"schema": 1}\n')
    outside = spool.parent / "outside.jsonl"
    outside.write_text(json.dumps({"schema": 1}) + "\n" + json.dumps(seen("r1")) + "\n")
    (spool / "0002.jsonl").symlink_to(outside)
    feeder.tick()
    (synthesized,) = events.buffer().drain("b")
    assert synthesized["undeclared"] == 1
    assert (spool / ".0001.jsonl.tmp").exists()
    assert outside.exists()
    assert not (spool / "0002.jsonl").exists()


def test_an_oversize_segment_is_refused_whole_and_counted_once(
    feeder: SpoolFeeder, spool: Path
) -> None:
    big = [seen(f"{n:04}" + "r" * 3000) for n in range(400)]
    assert 400 * 3000 > MAX_SEGMENT_BYTES
    seg = segment(spool, "0001.jsonl", *big)
    feeder.tick()
    (synthesized,) = events.buffer().drain("b")  # never a truncated prefix
    assert synthesized["undeclared"] == 1
    assert not seg.exists()


def test_a_record_over_the_byte_cap_is_refused(
    feeder: SpoolFeeder, spool: Path
) -> None:
    segment(spool, "0001.jsonl", seen("r1"), seen("x" * MAX_RECORD_BYTES))
    feeder.tick()
    synthesized, shipped = events.buffer().drain("b")
    assert (synthesized["undeclared"], shipped["record_id"]) == (1, "r1")


@needs_root
def test_a_segment_nobody_the_spool_trusts_owns_is_refused(
    feeder: SpoolFeeder, spool: Path
) -> None:
    seg = segment(spool, "0001.jsonl", seen("r1"))
    os.chown(seg, NOBODY, NOBODY)
    feeder.tick()
    (synthesized,) = events.buffer().drain("b")
    assert synthesized["undeclared"] == 1
    assert not seg.exists()


@needs_root
def test_a_segment_carrying_the_spool_group_is_read(
    spool: Path, feeder: SpoolFeeder
) -> None:
    """A setgid spool lets an emitter running as another user hand in work."""
    seg = segment(spool, "0001.jsonl", seen("r1"))
    os.chown(seg, NOBODY, spool.stat().st_gid)
    feeder.tick()
    assert [e["record_id"] for e in events.buffer().drain("b")] == ["r1"]


def test_a_directory_named_like_a_segment_is_counted_once(
    feeder: SpoolFeeder, spool: Path
) -> None:
    (spool / "0001.jsonl").mkdir()
    (spool / "0001.jsonl" / "keep").write_text("")
    for _ in range(3):
        feeder.tick()
    (synthesized,) = events.buffer().drain("b")
    assert synthesized["undeclared"] == 1


def test_a_segment_over_the_event_cap_is_refused_whole(
    feeder: SpoolFeeder, spool: Path
) -> None:
    seg = segment(
        spool, "0001.jsonl", *(seen(str(n)) for n in range(MAX_SEGMENT_EVENTS + 1))
    )
    feeder.tick()
    (synthesized,) = events.buffer().drain("b", max_events=10_000)
    assert synthesized["undeclared"] == MAX_SEGMENT_EVENTS + 1
    assert not seg.exists()


def test_lines_that_are_not_json_objects_are_refused_one_by_one(
    feeder: SpoolFeeder, spool: Path
) -> None:
    seg = spool / "0001.jsonl"
    lines = ['{"schema": 1}', "not json", "[1, 2]", json.dumps(seen("r1"))]
    seg.write_text("\n".join(lines) + "\n")
    feeder.tick()
    synthesized, shipped = events.buffer().drain("b")
    assert (synthesized["undeclared"], shipped["record_id"]) == (2, "r1")


@pytest.mark.parametrize(
    "header",
    ["[1]", "not json", "{}", '{"schema": true}', '{"schema": 1.0}'],
)
def test_a_header_that_is_not_a_schema_object_refuses_the_segment(
    feeder: SpoolFeeder, spool: Path, header: str
) -> None:
    """``true`` and ``1.0`` equal 1 in Python; only the integer is schema 1."""
    seg = spool / "0001.jsonl"
    seg.write_text(header + "\n" + json.dumps(seen("r1")) + "\n")
    feeder.tick()
    drained = events.buffer().drain("b")
    assert [e["kind"] for e in drained] == ["dropped_events"]
    assert not seg.exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 file")
def test_an_unreadable_segment_waits_for_the_next_tick(
    feeder: SpoolFeeder, spool: Path
) -> None:
    """Unreadable now is not refused: no count, no delete, read once it opens."""
    seg = segment(spool, "0001.jsonl", seen("r1"))
    seg.chmod(0o000)
    feeder.tick()
    assert seg.exists()
    assert events.buffer().drain("b1") == []
    seg.chmod(0o600)
    feeder.tick()
    assert [e["record_id"] for e in events.buffer().drain("b2")] == ["r1"]
