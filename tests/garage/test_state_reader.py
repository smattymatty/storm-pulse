"""GarageStateReader, the cadence-aware periodic garage read (CORE-005 decision 9).

Pins: a sweep cold and once per ``SWEEP_SECONDS``; between sweeps only hinted
buckets (at most 8 a call); topology every ``TOPOLOGY_EVERY`` producing calls;
targeted reads land in the cache; only a producing call advances a cadence.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

from stormpulse.garage.state import GarageStateReader
from tests.garage.state_reader_support import (
    FULL_ID,
    NODE_ID,
    Clock,
    hex_id,
    info,
    patched,
    reader_config,
)


def test_cold_first_call_reads_topology_and_buckets() -> None:
    reader = GarageStateReader()
    with patched() as m:
        state = reader.collect(reader_config())
    assert state is not None
    assert state.node_id == NODE_ID
    assert [b.id for b in state.buckets] == [FULL_ID]
    assert m["status"].call_count == 1
    assert m["list_buckets"].call_count == 1


def test_topology_cached_between_slow_multiple() -> None:
    reader = GarageStateReader(clock=Clock())
    with patched() as m:
        for _ in range(GarageStateReader.TOPOLOGY_EVERY):
            assert reader.collect(reader_config()) is not None
    # Topology read once (cold), reused for the rest of the window. The clock
    # never moves, so only the cold call sweeps; the rest serve the cache.
    assert m["status"].call_count == 1
    assert m["stats"].call_count == 1
    assert m["list_keys"].call_count == 1
    assert m["list_buckets"].call_count == 1


def test_topology_refreshed_on_slow_multiple() -> None:
    reader = GarageStateReader(clock=Clock())
    with patched() as m:
        for _ in range(GarageStateReader.TOPOLOGY_EVERY + 1):
            reader.collect(reader_config())
    # Cold read + one refresh when the window rolls over, sweep or not.
    assert m["status"].call_count == 2
    assert m["list_buckets"].call_count == 1


def test_bucket_walk_failure_skips_without_advancing_cadence() -> None:
    reader = GarageStateReader()
    # Cold call: topology reads fine, but the walk fails -> skip (None), and the
    # topology cadence must NOT advance off a skipped tick.
    failing_walk = MagicMock(return_value=(None, "ListBuckets unreachable"))
    with patched(list_buckets=failing_walk) as m:
        assert reader.collect(reader_config()) is None
        assert m["status"].call_count == 1
    # Next call succeeds and reuses the cached topology (status not re-read).
    with patched() as m2:
        assert reader.collect(reader_config()) is not None
        assert m2["status"].call_count == 0


def test_topology_failure_on_cold_call_returns_none() -> None:
    reader = GarageStateReader()
    failing_status = MagicMock(return_value=(None, "GetClusterStatus unreachable"))
    with patched(status=failing_status):
        # No cached topology to fall back on -> skip.
        assert reader.collect(reader_config()) is None


def test_due_topology_failure_reuses_cache() -> None:
    reader = GarageStateReader()
    with patched():
        assert reader.collect(reader_config()) is not None  # warm the cache
    # Make a refresh due, then fail it: the reader must reuse the cached
    # topology and still produce a state rather than skipping.
    with patched():
        for _ in range(GarageStateReader.TOPOLOGY_EVERY - 1):
            assert reader.collect(reader_config()) is not None
    failing_status = MagicMock(return_value=(None, "transient"))
    with patched(status=failing_status) as m:
        state = reader.collect(reader_config())
    assert state is not None
    assert state.node_id == NODE_ID
    assert m["status"].call_count == 1  # attempted, failed, fell back to cache
    # Still due: the next call retries.
    with patched() as m2:
        assert reader.collect(reader_config()) is not None
    assert m2["status"].call_count == 1


def test_unconfigured_returns_none_without_admin_calls() -> None:
    reader = GarageStateReader()
    with patched() as m:
        assert reader.collect(reader_config(admin_url="", admin_token="")) is None
    assert m["status"].call_count == 0
    assert m["list_buckets"].call_count == 0


def test_fresh_bypasses_both_cadences() -> None:
    # The on-demand garage_refresh path: an operator who just changed the
    # layout or a bucket must see it immediately, cadence notwithstanding.
    reader = GarageStateReader(clock=Clock())
    with patched() as m:
        assert reader.collect(reader_config()) is not None  # cold: reads all
        assert reader.collect(reader_config()) is not None  # cached
        assert m["status"].call_count == 1
        assert m["list_buckets"].call_count == 1
        assert reader.collect(reader_config(), fresh=True) is not None
        assert m["status"].call_count == 2  # forced re-read
        assert m["list_buckets"].call_count == 2  # forced sweep
        # The forced read reset both windows: the next periodic call caches.
        assert reader.collect(reader_config()) is not None
        assert m["status"].call_count == 2
        assert m["list_buckets"].call_count == 2


# ---------------------------------------------------------------------------
# The one-minute sweep
# ---------------------------------------------------------------------------


def test_sweep_due_at_sweep_seconds_not_before() -> None:
    clock = Clock()
    reader = GarageStateReader(clock=clock)
    with patched() as m:
        reader.collect(reader_config())
        clock.now = GarageStateReader.SWEEP_SECONDS - 0.1
        reader.collect(reader_config())
        assert m["list_buckets"].call_count == 1
        clock.now = GarageStateReader.SWEEP_SECONDS
        reader.collect(reader_config())
        assert m["list_buckets"].call_count == 2


def test_failed_sweep_stays_due_and_serves_nothing() -> None:
    clock = Clock()
    reader = GarageStateReader(clock=clock)
    with patched():
        reader.collect(reader_config())
    clock.now = GarageStateReader.SWEEP_SECONDS
    failing = MagicMock(return_value=(None, "ListBuckets unreachable"))
    with patched(list_buckets=failing):
        assert reader.collect(reader_config()) is None
    # Same instant, the sweep is still due: it retries rather than serve cache.
    with patched() as m:
        assert reader.collect(reader_config()) is not None
    assert m["list_buckets"].call_count == 1


# ---------------------------------------------------------------------------
# Targeted reads and the cache
# ---------------------------------------------------------------------------


def test_targeted_read_survives_the_next_non_sweep_call() -> None:
    # The periodic loop replaces runtime.state wholesale; a post-mutation merge
    # must already be in the cache it is replaced with.
    reader = GarageStateReader(clock=Clock())
    with patched() as m:
        reader.collect(reader_config())
        m["get_info"].side_effect = lambda **kw: (
            {**info(kw["bucket_ref"]), "bytes": 99},
            "",
        )
        reader.read_buckets(reader_config(), [FULL_ID])
        m["get_info"].side_effect = lambda **kw: (info(kw["bucket_ref"]), "")
        state = reader.collect(reader_config())
    assert state is not None
    assert [b.size_bytes for b in state.buckets] == [99]


def test_read_landing_mid_sweep_is_not_reverted_by_the_sweep() -> None:
    # A post-mutation read on another thread can land while a sweep walks; the
    # sweep's older read of that bucket must not win in the cache.
    other = hex_id(2)
    reader = GarageStateReader(clock=Clock())
    swept: list[str] = []

    def get_info(**kw: Any) -> tuple[dict[str, Any], str]:
        ref = kw["bucket_ref"]
        if swept == [FULL_ID] and ref == other:
            swept.append(ref)
            reader.read_buckets(
                reader_config(), [FULL_ID]
            )  # the mutation hook, mid-sweep
        elif ref == FULL_ID and swept == [FULL_ID, other]:
            return {**info(ref), "bytes": 99}, ""
        else:
            swept.append(ref)
        return info(ref), ""

    listed = MagicMock(return_value=([{"id": FULL_ID}, {"id": other}], ""))
    with patched(list_buckets=listed) as m:
        m["get_info"].side_effect = get_info
        swept_state = reader.collect(reader_config())
        m["get_info"].side_effect = lambda **kw: (info(kw["bucket_ref"]), "")
        state = reader.collect(reader_config())  # non-sweep: served from the cache
    assert swept_state is not None and state is not None
    sizes = {b.id: b.size_bytes for b in state.buckets}
    assert sizes[FULL_ID] == 99
    assert {b.id: b.size_bytes for b in swept_state.buckets}[FULL_ID] == 99


def test_read_affected_feeds_the_shared_reader() -> None:
    from stormpulse.garage import integration as garage_integration

    reader = garage_integration._state_reader
    with patched():
        reader.collect(reader_config())
        state = reader.collect(reader_config())
        assert state is not None
        garage_integration._read_affected(
            reader_config(), state, {"bucket_id": hex_id(5)}
        )
        state = reader.collect(reader_config())
    assert state is not None
    assert hex_id(5) in {b.id for b in state.buckets}


def test_targeted_read_before_any_sweep_is_dropped() -> None:
    reader = GarageStateReader(clock=Clock())
    with patched() as m:
        m["get_info"].side_effect = lambda **kw: (
            {**info(kw["bucket_ref"]), "bytes": 99},
            "",
        )
        assert [
            b.size_bytes for b in reader.read_buckets(reader_config(), [FULL_ID])
        ] == [99]
        m["get_info"].side_effect = lambda **kw: (info(kw["bucket_ref"]), "")
        state = reader.collect(reader_config())
    assert state is not None
    assert [b.size_bytes for b in state.buckets] == [1024]


def test_failed_sweeps_do_not_advance_the_topology_cadence() -> None:
    clock = Clock()
    reader = GarageStateReader(clock=clock)
    with patched():
        assert reader.collect(reader_config()) is not None  # cold: topology + sweep
    clock.now = GarageStateReader.SWEEP_SECONDS
    failing = MagicMock(return_value=(None, "ListBuckets unreachable"))
    with patched(list_buckets=failing):
        for _ in range(3):
            assert reader.collect(reader_config()) is None
    # Topology is owed after TOPOLOGY_EVERY producing calls, not attempts.
    with patched() as m:
        for _ in range(GarageStateReader.TOPOLOGY_EVERY - 1):
            assert reader.collect(reader_config()) is not None
        assert m["status"].call_count == 0
        assert reader.collect(reader_config()) is not None
    assert m["status"].call_count == 1


def test_sweep_drops_a_bucket_garage_no_longer_lists() -> None:
    # Targeted reads only upsert; the sweep is the one path a deletion takes.
    clock = Clock()
    reader = GarageStateReader(clock=clock)
    both = MagicMock(return_value=([{"id": FULL_ID}, {"id": hex_id(2)}], ""))
    with patched(list_buckets=both):
        assert reader.collect(reader_config()) is not None
    clock.now = GarageStateReader.SWEEP_SECONDS
    with patched():
        state = reader.collect(reader_config())
    assert state is not None
    assert [b.id for b in state.buckets] == [FULL_ID]
