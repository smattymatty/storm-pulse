"""GarageStateReader, the cadence-aware periodic garage read (CORE-005 decision 9).

Pins: a walk cold and once per ``REREAD_SECONDS``; a one-call membership
diff once per ``SWEEP_SECONDS``; between them only hinted buckets (at most 32 a
push); topology every ``TOPOLOGY_EVERY`` producing calls; targeted reads land
in the cache; only a producing call advances a cadence.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from stormpulse.garage.state_reader import GarageStateReader
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
        assert m["list_buckets"].call_count == 2  # forced walk
        # The forced read reset every window: the next periodic call caches.
        assert reader.collect(reader_config()) is not None
        assert m["status"].call_count == 2
        assert m["list_buckets"].call_count == 2


# ---------------------------------------------------------------------------
# The minute diff and the five-minute re-read
# ---------------------------------------------------------------------------

SWEEP = GarageStateReader.SWEEP_SECONDS
REREAD = GarageStateReader.REREAD_SECONDS


def test_diff_due_at_sweep_seconds_is_one_list_and_no_bucket_read() -> None:
    clock = Clock()
    reader = GarageStateReader(clock=clock)
    with patched() as m:
        reader.collect(reader_config())
        clock.now = SWEEP - 0.1
        reader.collect(reader_config())
        assert m["list_buckets"].call_count == 1
        clock.now = SWEEP
        reader.collect(reader_config())
        assert m["list_buckets"].call_count == 2
        assert m["get_info"].call_count == 1  # the cold walk's only


def test_reread_due_at_reread_seconds_walks_every_bucket() -> None:
    clock = Clock()
    reader = GarageStateReader(clock=clock)
    three = MagicMock(return_value=([{"id": hex_id(i)} for i in range(3)], ""))
    with patched(list_buckets=three) as m:
        reader.collect(reader_config())
        clock.now = REREAD - 0.1
        reader.collect(reader_config())  # a diff: lists, reads nothing
        assert m["get_info"].call_count == 3
        clock.now = REREAD
        reader.collect(reader_config())
        assert m["get_info"].call_count == 6


def test_quiet_five_minutes_cost_five_plus_n_admin_calls() -> None:
    # The cost the diff buys back: a hinted node with nothing to say spends
    # 1 call a minute, and 1 + N once in five, not 1 + N every minute.
    n = 4
    clock = Clock()
    reader = GarageStateReader(clock=clock)
    items = [{"id": hex_id(i)} for i in range(n)]
    with patched(list_buckets=MagicMock(return_value=(items, ""))):
        reader.collect(reader_config())  # cold walk, outside the window
    with patched(list_buckets=MagicMock(return_value=(items, ""))) as m:
        for minute in range(1, 6):
            clock.now = minute * SWEEP
            assert reader.collect(reader_config()) is not None
    assert m["list_buckets"].call_count + m["get_info"].call_count == 5 + n
    assert m["status"].call_count == 0  # topology rides ticks, not the clock


def test_reread_restarts_the_diff_cadence() -> None:
    clock = Clock()
    reader = GarageStateReader(clock=clock)
    with patched() as m:
        reader.collect(reader_config())
        clock.now = REREAD
        reader.collect(reader_config())  # the re-read
        clock.now = REREAD + SWEEP - 0.1
        reader.collect(reader_config())  # not a diff minute yet
        assert m["list_buckets"].call_count == 2
        clock.now = REREAD + SWEEP
        reader.collect(reader_config())
        assert m["list_buckets"].call_count == 3


def test_diff_does_not_restart_the_reread_cadence() -> None:
    clock = Clock()
    reader = GarageStateReader(clock=clock)
    with patched() as m:
        reader.collect(reader_config())
        clock.now = REREAD - SWEEP
        reader.collect(reader_config())  # a diff, one minute before the re-read
        clock.now = REREAD
        reader.collect(reader_config())
        assert m["get_info"].call_count == 2  # the re-read still came on time


def test_failed_diff_serves_the_cache_and_stays_due() -> None:
    clock = Clock()
    reader = GarageStateReader(clock=clock)
    with patched():
        reader.collect(reader_config())
    clock.now = SWEEP
    failing = MagicMock(return_value=(None, "ListBuckets unreachable"))
    with patched(list_buckets=failing):
        state = reader.collect(reader_config())
    assert state is not None
    assert [b.id for b in state.buckets] == [FULL_ID]
    # Same instant, the diff is still due: it retries.
    with patched() as m:
        assert reader.collect(reader_config()) is not None
    assert m["list_buckets"].call_count == 1
    with patched() as m2:
        assert reader.collect(reader_config()) is not None
    assert m2["list_buckets"].call_count == 0  # the producing diff marked it


def test_diff_reads_a_new_bucket_with_one_get_info() -> None:
    # A bucket created over S3 is in the state within a minute, read once;
    # the N buckets the cache already holds are not re-read.
    clock = Clock()
    reader = GarageStateReader(clock=clock)
    old = [FULL_ID, hex_id(2), hex_id(3)]
    with patched(list_buckets=MagicMock(return_value=([{"id": i} for i in old], ""))):
        reader.collect(reader_config())
    clock.now = SWEEP
    grown = [*old, hex_id(4)]
    with patched(
        list_buckets=MagicMock(return_value=([{"id": i} for i in grown], ""))
    ) as m:
        state = reader.collect(reader_config())
    assert state is not None
    assert [b.id for b in state.buckets] == grown
    assert [c.kwargs["bucket_ref"] for c in m["get_info"].call_args_list] == [hex_id(4)]


def test_diff_drops_a_bucket_garage_no_longer_lists_with_no_read() -> None:
    # A bucket deleted over S3 leaves the state within a minute at the cost
    # of the one ``ListBuckets``; nothing is read to notice it.
    clock = Clock()
    reader = GarageStateReader(clock=clock)
    both = MagicMock(return_value=([{"id": FULL_ID}, {"id": hex_id(2)}], ""))
    with patched(list_buckets=both):
        reader.collect(reader_config())
    clock.now = SWEEP
    with patched() as m:
        state = reader.collect(reader_config())
    assert state is not None
    assert [b.id for b in state.buckets] == [FULL_ID]
    assert m["get_info"].call_count == 0


def test_failed_reread_stays_due_and_serves_nothing() -> None:
    clock = Clock()
    reader = GarageStateReader(clock=clock)
    with patched():
        reader.collect(reader_config())
    clock.now = REREAD
    failing = MagicMock(return_value=(None, "ListBuckets unreachable"))
    with patched(list_buckets=failing):
        assert reader.collect(reader_config()) is None
    # Same instant, the re-read is still due: it retries rather than serve cache.
    with patched() as m:
        assert reader.collect(reader_config()) is not None
    assert m["list_buckets"].call_count == 1
    assert m["get_info"].call_count == 1


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


def test_read_landing_mid_diff_is_not_dropped_by_the_diff() -> None:
    # A bucket created while the diff's ``ListBuckets`` is in flight is read by
    # the mutation hook before the list returns without it; the diff must keep
    # it, not drop it as unlisted.
    created = hex_id(2)
    clock = Clock()
    reader = GarageStateReader(clock=clock)
    with patched():
        reader.collect(reader_config())
    clock.now = SWEEP

    def list_buckets(**kw: Any) -> tuple[list[dict[str, str]], str]:
        reader.read_buckets(reader_config(), [created])  # the hook, mid-list
        return [{"id": FULL_ID}], ""

    with patched(list_buckets=MagicMock(side_effect=list_buckets)) as m:
        state = reader.collect(reader_config())
        assert m["get_info"].call_count == 1  # the hook's read, no diff re-read
    assert state is not None
    assert [b.id for b in state.buckets] == [FULL_ID, created]


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


def test_failed_rereads_do_not_advance_the_topology_cadence() -> None:
    clock = Clock()
    reader = GarageStateReader(clock=clock)
    with patched():
        assert reader.collect(reader_config()) is not None  # cold: topology + walk
    clock.now = REREAD
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


def test_reread_drops_a_bucket_garage_no_longer_lists() -> None:
    # Targeted reads only upsert; a walk is where a deletion leaves the cache.
    clock = Clock()
    reader = GarageStateReader(clock=clock)
    both = MagicMock(return_value=([{"id": FULL_ID}, {"id": hex_id(2)}], ""))
    with patched(list_buckets=both):
        assert reader.collect(reader_config()) is not None
    clock.now = REREAD
    with patched():
        state = reader.collect(reader_config())
    assert state is not None
    assert [b.id for b in state.buckets] == [FULL_ID]


def test_reread_sheds_a_bucket_whose_info_read_failed() -> None:
    # A re-read rebuilds the cache from what it read: a bucket still listed
    # but unreadable is not carried over stale from the last walk.
    clock = Clock()
    reader = GarageStateReader(clock=clock)
    both = MagicMock(return_value=([{"id": FULL_ID}, {"id": hex_id(2)}], ""))
    with patched(list_buckets=both):
        assert reader.collect(reader_config()) is not None
    clock.now = REREAD
    failing = MagicMock(
        side_effect=lambda **kw: (
            (None, "down") if kw["bucket_ref"] == hex_id(2) else (info(FULL_ID), "")
        )
    )
    with (
        patched(list_buckets=both),
        patch("stormpulse.garage.state.admin_api.get_bucket_info", failing),
    ):
        state = reader.collect(reader_config())
    assert state is not None
    assert [b.id for b in state.buckets] == [FULL_ID]
