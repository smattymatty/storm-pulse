"""GarageStateReader, the cadence-aware periodic garage read (CORE-005 decision 9).

Pins: a sweep cold and once per ``SWEEP_SECONDS``; between sweeps only hinted
buckets (at most 8 a call); topology every ``TOPOLOGY_EVERY`` producing calls;
targeted reads land in the cache; only a producing call advances a cadence.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from stormpulse import events
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.state import MAX_TARGETED_BUCKET_READS, GarageStateReader

ADMIN_URL = "http://127.0.0.1:3903"
FULL_ID = "f1dc32249aa1d80a" + "0" * 48
NODE_ID = "a8bfb94f8a2786f74c227c75a690846b915560c08dc8a0c8681b980082d0a4b9"


def _config(
    *,
    admin_url: str = ADMIN_URL,
    admin_token: str = "tok",
    hint_file: str = "/nonexistent/hint.json",
) -> GarageConfig:
    return GarageConfig(
        enabled=True,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=Path("/tmp/garage.toml"),
        admin_url=admin_url,
        admin_token=admin_token,
        hint_file=hint_file,
    )


class _Clock:
    """Injectable monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _node() -> dict[str, Any]:
    return {
        "id": NODE_ID,
        "hostname": "garage-one",
        "addr": "10.0.0.1:3901",
        "garageVersion": "v2.3.0",
        "isUp": True,
        "role": {"zone": "canada-1", "capacity": 3_000_000_000_000, "tags": []},
        "dataPartition": {"available": 2_800_000_000_000, "total": 3_000_000_000_000},
    }


def _info(bucket_id: str = FULL_ID) -> dict[str, Any]:
    return {
        "id": bucket_id,
        "globalAliases": ["media"],
        "websiteAccess": False,
        "websiteConfig": None,
        "keys": [],
        "objects": 3,
        "bytes": 1024,
        "quotas": {"maxSize": None, "maxObjects": None},
    }


@contextmanager
def _patched(
    *,
    status: MagicMock | None = None,
    list_buckets: MagicMock | None = None,
) -> Iterator[dict[str, MagicMock]]:
    """Patch the five admin reads with counting mocks; override status/list_buckets to inject failures."""
    status = status or MagicMock(return_value=({"nodes": [_node()]}, ""))
    list_buckets = list_buckets or MagicMock(return_value=([{"id": FULL_ID}], ""))
    stats = MagicMock(return_value=({"totalObjectCount": 5}, ""))
    list_keys = MagicMock(return_value=([{"id": "GKabc", "name": "k"}], ""))
    get_info = MagicMock(side_effect=lambda **kw: (_info(kw["bucket_ref"]), ""))
    with (
        patch("stormpulse.garage.state.admin_api.get_cluster_status", status),
        patch("stormpulse.garage.state.admin_api.get_cluster_statistics", stats),
        patch("stormpulse.garage.state.admin_api.list_keys", list_keys),
        patch("stormpulse.garage.state.admin_api.list_buckets", list_buckets),
        patch("stormpulse.garage.state.admin_api.get_bucket_info", get_info),
    ):
        yield {
            "status": status,
            "stats": stats,
            "list_keys": list_keys,
            "list_buckets": list_buckets,
            "get_info": get_info,
        }


def test_cold_first_call_reads_topology_and_buckets() -> None:
    reader = GarageStateReader()
    with _patched() as m:
        state = reader.collect(_config())
    assert state is not None
    assert state.node_id == NODE_ID
    assert [b.id for b in state.buckets] == [FULL_ID]
    assert m["status"].call_count == 1
    assert m["list_buckets"].call_count == 1


def test_topology_cached_between_slow_multiple() -> None:
    reader = GarageStateReader(clock=_Clock())
    with _patched() as m:
        for _ in range(GarageStateReader.TOPOLOGY_EVERY):
            assert reader.collect(_config()) is not None
    # Topology read once (cold), reused for the rest of the window. The clock
    # never moves, so only the cold call sweeps; the rest serve the cache.
    assert m["status"].call_count == 1
    assert m["stats"].call_count == 1
    assert m["list_keys"].call_count == 1
    assert m["list_buckets"].call_count == 1


def test_topology_refreshed_on_slow_multiple() -> None:
    reader = GarageStateReader(clock=_Clock())
    with _patched() as m:
        for _ in range(GarageStateReader.TOPOLOGY_EVERY + 1):
            reader.collect(_config())
    # Cold read + one refresh when the window rolls over, sweep or not.
    assert m["status"].call_count == 2
    assert m["list_buckets"].call_count == 1


def test_bucket_walk_failure_skips_without_advancing_cadence() -> None:
    reader = GarageStateReader()
    # Cold call: topology reads fine, but the walk fails -> skip (None), and the
    # topology cadence must NOT advance off a skipped tick.
    failing_walk = MagicMock(return_value=(None, "ListBuckets unreachable"))
    with _patched(list_buckets=failing_walk) as m:
        assert reader.collect(_config()) is None
        assert m["status"].call_count == 1
    # Next call succeeds and reuses the cached topology (status not re-read).
    with _patched() as m2:
        assert reader.collect(_config()) is not None
        assert m2["status"].call_count == 0


def test_topology_failure_on_cold_call_returns_none() -> None:
    reader = GarageStateReader()
    failing_status = MagicMock(return_value=(None, "GetClusterStatus unreachable"))
    with _patched(status=failing_status):
        # No cached topology to fall back on -> skip.
        assert reader.collect(_config()) is None


def test_due_topology_failure_reuses_cache() -> None:
    reader = GarageStateReader()
    with _patched():
        assert reader.collect(_config()) is not None  # warm the cache
    # Make a refresh due, then fail it: the reader must reuse the cached
    # topology and still produce a state rather than skipping.
    with _patched():
        for _ in range(GarageStateReader.TOPOLOGY_EVERY - 1):
            assert reader.collect(_config()) is not None
    failing_status = MagicMock(return_value=(None, "transient"))
    with _patched(status=failing_status) as m:
        state = reader.collect(_config())
    assert state is not None
    assert state.node_id == NODE_ID
    assert m["status"].call_count == 1  # attempted, failed, fell back to cache
    # Still due: the next call retries.
    with _patched() as m2:
        assert reader.collect(_config()) is not None
    assert m2["status"].call_count == 1


def test_unconfigured_returns_none_without_admin_calls() -> None:
    reader = GarageStateReader()
    with _patched() as m:
        assert reader.collect(_config(admin_url="", admin_token="")) is None
    assert m["status"].call_count == 0
    assert m["list_buckets"].call_count == 0


def test_fresh_bypasses_both_cadences() -> None:
    # The on-demand garage_refresh path: an operator who just changed the
    # layout or a bucket must see it immediately, cadence notwithstanding.
    reader = GarageStateReader(clock=_Clock())
    with _patched() as m:
        assert reader.collect(_config()) is not None  # cold: reads all
        assert reader.collect(_config()) is not None  # cached
        assert m["status"].call_count == 1
        assert m["list_buckets"].call_count == 1
        assert reader.collect(_config(), fresh=True) is not None
        assert m["status"].call_count == 2  # forced re-read
        assert m["list_buckets"].call_count == 2  # forced sweep
        # The forced read reset both windows: the next periodic call caches.
        assert reader.collect(_config()) is not None
        assert m["status"].call_count == 2
        assert m["list_buckets"].call_count == 2


# ---------------------------------------------------------------------------
# The one-minute sweep
# ---------------------------------------------------------------------------


def test_sweep_due_at_sweep_seconds_not_before() -> None:
    clock = _Clock()
    reader = GarageStateReader(clock=clock)
    with _patched() as m:
        reader.collect(_config())
        clock.now = GarageStateReader.SWEEP_SECONDS - 0.1
        reader.collect(_config())
        assert m["list_buckets"].call_count == 1
        clock.now = GarageStateReader.SWEEP_SECONDS
        reader.collect(_config())
        assert m["list_buckets"].call_count == 2


def test_failed_sweep_stays_due_and_serves_nothing() -> None:
    clock = _Clock()
    reader = GarageStateReader(clock=clock)
    with _patched():
        reader.collect(_config())
    clock.now = GarageStateReader.SWEEP_SECONDS
    failing = MagicMock(return_value=(None, "ListBuckets unreachable"))
    with _patched(list_buckets=failing):
        assert reader.collect(_config()) is None
    # Same instant, the sweep is still due: it retries rather than serve cache.
    with _patched() as m:
        assert reader.collect(_config()) is not None
    assert m["list_buckets"].call_count == 1


# ---------------------------------------------------------------------------
# Hinted reads between sweeps
# ---------------------------------------------------------------------------


def _hex(i: int) -> str:
    return f"{i:064x}"


def _hint(tmp_path: Path, ids: list[str], *, written_at: float | None = None) -> str:
    path = tmp_path / "hint.json"
    doc = {"version": 1, "written_at": written_at or time.time(), "bucket_ids": ids}
    path.write_text(json.dumps(doc))
    os.chmod(path, 0o600)
    return str(path)


def _warm(reader: GarageStateReader, config: GarageConfig) -> None:
    """Cold sweep with an empty hint, so later calls are hint-only."""
    with _patched():
        assert reader.collect(config) is not None


def test_hinted_bucket_is_read_and_merged_between_sweeps(tmp_path: Path) -> None:
    reader = GarageStateReader(clock=_Clock())
    hint = _hint(tmp_path, [])
    _warm(reader, _config(hint_file=hint))
    _hint(tmp_path, [_hex(7)])
    with _patched() as m:
        state = reader.collect(_config(hint_file=hint))
    assert state is not None
    assert m["list_buckets"].call_count == 0  # no sweep
    assert [c.kwargs["bucket_ref"] for c in m["get_info"].call_args_list] == [_hex(7)]
    # A newcomer is appended to the cached set, never replacing it.
    assert [b.id for b in state.buckets] == [FULL_ID, _hex(7)]


def test_hint_batch_capped_per_push_rest_stays_pending(tmp_path: Path) -> None:
    reader = GarageStateReader(clock=_Clock())
    hint = _hint(tmp_path, [])
    _warm(reader, _config(hint_file=hint))
    ids = [_hex(i) for i in range(1, MAX_TARGETED_BUCKET_READS + 4)]
    _hint(tmp_path, ids)
    with _patched() as m:
        reader.collect(_config(hint_file=hint))
        assert m["get_info"].call_count == MAX_TARGETED_BUCKET_READS
        _hint(tmp_path, [])  # the writer moved on; the overflow is still owed
        state = reader.collect(_config(hint_file=hint))
    assert m["get_info"].call_count == len(ids)
    assert state is not None
    assert {b.id for b in state.buckets} == {FULL_ID, *ids}


def test_sweep_clears_pending(tmp_path: Path) -> None:
    clock = _Clock()
    reader = GarageStateReader(clock=clock)
    hint = _hint(tmp_path, [])
    _warm(reader, _config(hint_file=hint))
    _hint(tmp_path, [_hex(i) for i in range(1, MAX_TARGETED_BUCKET_READS + 4)])
    clock.now = GarageStateReader.SWEEP_SECONDS
    with _patched():
        reader.collect(_config(hint_file=hint))  # sweep: reads every bucket
    _hint(tmp_path, [])
    with _patched() as m:
        reader.collect(_config(hint_file=hint))
    assert m["get_info"].call_count == 0


def test_targeted_read_survives_the_next_non_sweep_call() -> None:
    # The periodic loop replaces runtime.state wholesale; a post-mutation merge
    # must already be in the cache it is replaced with.
    reader = GarageStateReader(clock=_Clock())
    with _patched() as m:
        reader.collect(_config())
        m["get_info"].side_effect = lambda **kw: (
            {**_info(kw["bucket_ref"]), "bytes": 99},
            "",
        )
        reader.read(_config(), [FULL_ID])
        m["get_info"].side_effect = lambda **kw: (_info(kw["bucket_ref"]), "")
        state = reader.collect(_config())
    assert state is not None
    assert [b.size_bytes for b in state.buckets] == [99]


def test_read_landing_mid_sweep_is_not_reverted_by_the_sweep() -> None:
    # A post-mutation read on another thread can land while a sweep walks; the
    # sweep's older read of that bucket must not win in the cache.
    other = _hex(2)
    reader = GarageStateReader(clock=_Clock())
    swept: list[str] = []

    def get_info(**kw: Any) -> tuple[dict[str, Any], str]:
        ref = kw["bucket_ref"]
        if swept == [FULL_ID] and ref == other:
            swept.append(ref)
            reader.read(_config(), [FULL_ID])  # the mutation hook, mid-sweep
        elif ref == FULL_ID and swept == [FULL_ID, other]:
            return {**_info(ref), "bytes": 99}, ""
        else:
            swept.append(ref)
        return _info(ref), ""

    listed = MagicMock(return_value=([{"id": FULL_ID}, {"id": other}], ""))
    with _patched(list_buckets=listed) as m:
        m["get_info"].side_effect = get_info
        swept_state = reader.collect(_config())
        m["get_info"].side_effect = lambda **kw: (_info(kw["bucket_ref"]), "")
        state = reader.collect(_config())  # non-sweep: served from the cache
    assert swept_state is not None and state is not None
    sizes = {b.id: b.size_bytes for b in state.buckets}
    assert sizes[FULL_ID] == 99
    assert {b.id: b.size_bytes for b in swept_state.buckets}[FULL_ID] == 99


def test_read_affected_feeds_the_shared_reader() -> None:
    from stormpulse.garage import integration as garage_integration

    reader = garage_integration._state_reader
    with _patched():
        reader.collect(_config())
        state = reader.collect(_config())
        assert state is not None
        garage_integration._read_affected(_config(), state, {"bucket_id": _hex(5)})
        state = reader.collect(_config())
    assert state is not None
    assert _hex(5) in {b.id for b in state.buckets}


def test_targeted_read_before_any_sweep_is_dropped() -> None:
    reader = GarageStateReader(clock=_Clock())
    with _patched() as m:
        m["get_info"].side_effect = lambda **kw: (
            {**_info(kw["bucket_ref"]), "bytes": 99},
            "",
        )
        assert [b.size_bytes for b in reader.read(_config(), [FULL_ID])] == [99]
        m["get_info"].side_effect = lambda **kw: (_info(kw["bucket_ref"]), "")
        state = reader.collect(_config())
    assert state is not None
    assert [b.size_bytes for b in state.buckets] == [1024]


def test_empty_hint_tick_makes_no_admin_call_and_no_event(tmp_path: Path) -> None:
    reader = GarageStateReader(clock=_Clock())
    hint = _hint(tmp_path, [])
    _warm(reader, _config(hint_file=hint))
    events.buffer().drain("warm")
    with _patched() as m:
        assert reader.collect(_config(hint_file=hint)) is not None
    assert sum(mock.call_count for mock in m.values()) == 0
    assert events.buffer().drain("b1") == []


def test_refusal_logged_once_per_reason_until_a_good_read(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    reader = GarageStateReader(clock=_Clock())
    hint = str(tmp_path / "hint.json")
    config = _config(hint_file=hint)
    caplog.set_level(logging.DEBUG, logger="stormpulse.garage.state")

    def refusals() -> list[logging.LogRecord]:
        return [r for r in caplog.records if "Ignoring hint file" in r.getMessage()]

    with _patched():
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
    reader = GarageStateReader(clock=_Clock())
    with _patched(), patch("stormpulse.garage.state.read_hint") as read_hint:
        reader.collect(_config(hint_file=""))
        reader.collect(_config(hint_file=""))
    assert read_hint.call_count == 0


def test_no_hint_file_walks_every_bucket_every_call() -> None:
    # The self-hosted default: no hint writer, so every push walks, as before.
    reader = GarageStateReader(clock=_Clock())
    with _patched() as m:
        for _ in range(3):
            assert reader.collect(_config(hint_file="")) is not None
    assert m["list_buckets"].call_count == 3


def test_failed_sweeps_do_not_advance_the_topology_cadence() -> None:
    clock = _Clock()
    reader = GarageStateReader(clock=clock)
    with _patched():
        assert reader.collect(_config()) is not None  # cold: topology + sweep
    clock.now = GarageStateReader.SWEEP_SECONDS
    failing = MagicMock(return_value=(None, "ListBuckets unreachable"))
    with _patched(list_buckets=failing):
        for _ in range(3):
            assert reader.collect(_config()) is None
    # Topology is owed after TOPOLOGY_EVERY producing calls, not attempts.
    with _patched() as m:
        for _ in range(GarageStateReader.TOPOLOGY_EVERY - 1):
            assert reader.collect(_config()) is not None
        assert m["status"].call_count == 0
        assert reader.collect(_config()) is not None
    assert m["status"].call_count == 1


def test_sweep_drops_a_bucket_garage_no_longer_lists() -> None:
    # Targeted reads only upsert; the sweep is the one path a deletion takes.
    clock = _Clock()
    reader = GarageStateReader(clock=clock)
    both = MagicMock(return_value=([{"id": FULL_ID}, {"id": _hex(2)}], ""))
    with _patched(list_buckets=both):
        assert reader.collect(_config()) is not None
    clock.now = GarageStateReader.SWEEP_SECONDS
    with _patched():
        state = reader.collect(_config())
    assert state is not None
    assert [b.id for b in state.buckets] == [FULL_ID]
