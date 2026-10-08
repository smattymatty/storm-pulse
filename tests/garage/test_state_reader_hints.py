"""GarageStateReader with a hint file: between walks only the hinted buckets
are read (at most 32 a push), the overflow stays pending until a walk, and a
refused file is logged once per reason. No hint file walks every call.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from stormpulse import events
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.state import cap_targeted_reads
from stormpulse.garage.state_reader import MAX_HINTED_READS, GarageStateReader
from tests.garage.state_reader_support import (
    FULL_ID,
    Clock,
    hex_id,
    patched,
    reader_config,
)


def _hint(tmp_path: Path, ids: list[str], *, written_at: float | None = None) -> str:
    path = tmp_path / "hint.json"
    doc = {"version": 1, "written_at": written_at or time.time(), "bucket_ids": ids}
    path.write_text(json.dumps(doc))
    os.chmod(path, 0o600)
    return str(path)


def _warm(reader: GarageStateReader, config: GarageConfig) -> None:
    """Cold sweep with an empty hint, so later calls are hint-only."""
    with patched():
        assert reader.collect(config) is not None


def test_hinted_bucket_is_read_and_merged_between_sweeps(tmp_path: Path) -> None:
    reader = GarageStateReader(clock=Clock())
    hint = _hint(tmp_path, [])
    _warm(reader, reader_config(hint_file=hint))
    _hint(tmp_path, [hex_id(7)])
    with patched() as m:
        state = reader.collect(reader_config(hint_file=hint))
    assert state is not None
    assert m["list_buckets"].call_count == 0  # no sweep
    assert [c.kwargs["bucket_ref"] for c in m["get_info"].call_args_list] == [hex_id(7)]
    # A newcomer is appended to the cached set, never replacing it.
    assert [b.id for b in state.buckets] == [FULL_ID, hex_id(7)]


def test_hint_batch_capped_per_push_rest_stays_pending(tmp_path: Path) -> None:
    reader = GarageStateReader(clock=Clock())
    hint = _hint(tmp_path, [])
    _warm(reader, reader_config(hint_file=hint))
    ids = [hex_id(i) for i in range(1, MAX_HINTED_READS + 4)]
    _hint(tmp_path, ids)
    with patched() as m:
        reader.collect(reader_config(hint_file=hint))
        assert m["get_info"].call_count == MAX_HINTED_READS
        _hint(tmp_path, [])  # the writer moved on; the overflow is still owed
        state = reader.collect(reader_config(hint_file=hint))
    assert m["get_info"].call_count == len(ids)
    assert state is not None
    assert {b.id for b in state.buckets} == {FULL_ID, *ids}


def test_reread_clears_pending(tmp_path: Path) -> None:
    clock = Clock()
    reader = GarageStateReader(clock=clock)
    hint = _hint(tmp_path, [])
    _warm(reader, reader_config(hint_file=hint))
    _hint(tmp_path, [hex_id(i) for i in range(1, MAX_HINTED_READS + 4)])
    clock.now = GarageStateReader.REREAD_SECONDS
    with patched():
        reader.collect(reader_config(hint_file=hint))  # re-read: every bucket
    _hint(tmp_path, [])
    with patched() as m:
        reader.collect(reader_config(hint_file=hint))
    assert m["get_info"].call_count == 0


def test_diff_push_drains_hints_and_leaves_the_overflow_pending(
    tmp_path: Path,
) -> None:
    # A diff satisfies no hint: its push drains a batch after the one
    # ``ListBuckets``, and the overflow is still owed on the next push.
    clock = Clock()
    reader = GarageStateReader(clock=clock)
    hint = _hint(tmp_path, [])
    _warm(reader, reader_config(hint_file=hint))
    ids = [hex_id(i) for i in range(1, MAX_HINTED_READS + 4)]
    _hint(tmp_path, ids)
    clock.now = GarageStateReader.SWEEP_SECONDS
    with patched() as m:
        reader.collect(reader_config(hint_file=hint))  # the diff minute
        assert m["list_buckets"].call_count == 1
        assert m["get_info"].call_count == MAX_HINTED_READS
        _hint(tmp_path, [])
        reader.collect(reader_config(hint_file=hint))
    assert [c.kwargs["bucket_ref"] for c in m["get_info"].call_args_list] == ids


def test_failed_diff_minute_still_drains_hints(tmp_path: Path) -> None:
    # A diff whose ``ListBuckets`` fails serves the cache like a hinted push,
    # so the hinted bucket is still read that minute, not owed until the next.
    clock = Clock()
    reader = GarageStateReader(clock=clock)
    hint = _hint(tmp_path, [])
    _warm(reader, reader_config(hint_file=hint))
    _hint(tmp_path, [hex_id(7)])
    clock.now = GarageStateReader.SWEEP_SECONDS
    failing = MagicMock(return_value=(None, "ListBuckets unreachable"))
    with patched(list_buckets=failing) as m:
        state = reader.collect(reader_config(hint_file=hint))
    assert state is not None
    assert [c.kwargs["bucket_ref"] for c in m["get_info"].call_args_list] == [hex_id(7)]
    assert [b.id for b in state.buckets] == [FULL_ID, hex_id(7)]


def test_forty_hinted_ids_drain_in_two_pushes(tmp_path: Path) -> None:
    # The hint cap is its own 32, not the post-mutation 8: 40 ids take two
    # pushes, and a post-mutation fan-out of 40 still reads 8.
    reader = GarageStateReader(clock=Clock())
    hint = _hint(tmp_path, [])
    _warm(reader, reader_config(hint_file=hint))
    ids = [hex_id(i) for i in range(1, 41)]
    _hint(tmp_path, ids)
    with patched() as m:
        reader.collect(reader_config(hint_file=hint))
        assert m["get_info"].call_count == 32
        _hint(tmp_path, [])
        reader.collect(reader_config(hint_file=hint))
        assert m["get_info"].call_count == 40
        reader.collect(reader_config(hint_file=hint))
    assert m["get_info"].call_count == 40  # nothing owed after two pushes
    assert len(cap_targeted_reads(ids, context="Post-mutation")) == 8


def test_empty_hint_tick_makes_no_admin_call_and_no_event(tmp_path: Path) -> None:
    reader = GarageStateReader(clock=Clock())
    hint = _hint(tmp_path, [])
    _warm(reader, reader_config(hint_file=hint))
    events.buffer().drain("warm")
    with patched() as m:
        assert reader.collect(reader_config(hint_file=hint)) is not None
    assert sum(mock.call_count for mock in m.values()) == 0
    assert events.buffer().drain("b1") == []


def test_refusal_logged_once_per_reason_until_a_good_read(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    reader = GarageStateReader(clock=Clock())
    hint = str(tmp_path / "hint.json")
    config = reader_config(hint_file=hint)
    caplog.set_level(logging.DEBUG, logger="stormpulse.garage.state_reader")

    def refusals() -> list[logging.LogRecord]:
        return [r for r in caplog.records if "Ignoring hint file" in r.getMessage()]

    with patched():
        reader.collect(config)  # missing: unreadable
        reader.collect(config)  # same reason: silent
        assert len(refusals()) == 1
        assert refusals()[0].levelno == logging.WARNING
        _hint(tmp_path, [], written_at=time.time() - 3600)
        reader.collect(config)  # new reason: stale
        assert len(refusals()) == 2
        assert refusals()[1].levelno == logging.DEBUG
        _hint(tmp_path, [])
        reader.collect(config)  # good read resets
        _hint(tmp_path, [], written_at=time.time() - 3600)
        reader.collect(config)  # stale again: logged
    assert len(refusals()) == 3


def test_no_hint_file_means_no_hint_read() -> None:
    reader = GarageStateReader(clock=Clock())
    with patched(), patch("stormpulse.garage.state_reader.read_hint") as read_hint:
        reader.collect(reader_config(hint_file=""))
        reader.collect(reader_config(hint_file=""))
    assert read_hint.call_count == 0


def test_no_hint_file_walks_every_bucket_every_call() -> None:
    # The self-hosted default: no hint writer, so every push walks, as before.
    reader = GarageStateReader(clock=Clock())
    with patched() as m:
        for _ in range(3):
            assert reader.collect(reader_config(hint_file="")) is not None
    assert m["list_buckets"].call_count == 3


def test_drain_reads_in_the_writers_order_latest_file_first(tmp_path: Path) -> None:
    # The writer lists the most recently touched first and the cap keeps those;
    # a sorted drain would starve every id past the first thirty-two by hex prefix.
    reader = GarageStateReader(clock=Clock())
    hint = _hint(tmp_path, [])
    _warm(reader, reader_config(hint_file=hint))
    _hint(tmp_path, [hex_id(0xF), hex_id(0xE), hex_id(0xD)])
    with patched() as m:
        reader.collect(reader_config(hint_file=hint))
        assert [c.kwargs["bucket_ref"] for c in m["get_info"].call_args_list] == [
            hex_id(0xF),
            hex_id(0xE),
            hex_id(0xD),
        ]
    # A later file leads; an id it drops but the reader still owes keeps its turn.
    ids = [hex_id(i) for i in range(1, MAX_HINTED_READS + 3)]
    _hint(tmp_path, ids)
    with patched():
        reader.collect(reader_config(hint_file=hint))  # drains ids[:32]; owes 2
    _hint(tmp_path, [hex_id(0xB)])
    with patched() as m:
        reader.collect(reader_config(hint_file=hint))
    assert [c.kwargs["bucket_ref"] for c in m["get_info"].call_args_list] == [
        hex_id(0xB),
        *ids[MAX_HINTED_READS:],
    ]
