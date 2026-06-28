"""Tests for GarageStateReader: the cadence-aware periodic garage read.

The reader composes three freshness needs behind CORE-005's single
``collect_state`` interval (capacity-model 2026-06-27 amendment):

- per-bucket usage walk on EVERY call (pinned to the push cadence),
- topology (status + statistics + key inventory) on the cold first call and
  then once every ``TOPOLOGY_EVERY`` calls, reusing the cache in between,
- a failed topology refresh reuses the cache and stays due (retries next call),
- a failed bucket walk skips the tick (returns None) without advancing cadence.

Call counts are the contract here, so every admin read is a counting mock.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from stormpulse.garage.config import GarageConfig
from stormpulse.garage.state import GarageStateReader

ADMIN_URL = "http://127.0.0.1:3903"
FULL_ID = "f1dc32249aa1d80a" + "0" * 48
NODE_ID = "a8bfb94f8a2786f74c227c75a690846b915560c08dc8a0c8681b980082d0a4b9"


def _config(*, admin_url: str = ADMIN_URL, admin_token: str = "tok") -> GarageConfig:
    return GarageConfig(
        enabled=True,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=Path("/tmp/garage.toml"),
        admin_url=admin_url,
        admin_token=admin_token,
    )


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


def _info() -> dict[str, Any]:
    return {
        "id": FULL_ID,
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
    get_info = MagicMock(return_value=(_info(), ""))
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
    reader = GarageStateReader()
    with _patched() as m:
        for _ in range(GarageStateReader.TOPOLOGY_EVERY):
            assert reader.collect(_config()) is not None
    # Topology read once (cold), reused for the rest of the window; the bucket
    # walk runs every single call.
    assert m["status"].call_count == 1
    assert m["stats"].call_count == 1
    assert m["list_keys"].call_count == 1
    assert m["list_buckets"].call_count == GarageStateReader.TOPOLOGY_EVERY


def test_topology_refreshed_on_slow_multiple() -> None:
    reader = GarageStateReader()
    with _patched() as m:
        for _ in range(GarageStateReader.TOPOLOGY_EVERY + 1):
            reader.collect(_config())
    # Cold read + one refresh when the window rolls over.
    assert m["status"].call_count == 2
    assert m["list_buckets"].call_count == GarageStateReader.TOPOLOGY_EVERY + 1


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
    # Force a refresh to be due, then fail it: the reader must reuse the cached
    # topology and still produce a state rather than skipping.
    reader._since_topology = GarageStateReader.TOPOLOGY_EVERY
    failing_status = MagicMock(return_value=(None, "transient"))
    with _patched(status=failing_status) as m:
        state = reader.collect(_config())
    assert state is not None
    assert state.node_id == NODE_ID
    assert m["status"].call_count == 1  # attempted, failed, fell back to cache


def test_unconfigured_returns_none_without_admin_calls() -> None:
    reader = GarageStateReader()
    with _patched() as m:
        assert reader.collect(_config(admin_url="", admin_token="")) is None
    assert m["status"].call_count == 0
    assert m["list_buckets"].call_count == 0
