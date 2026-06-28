"""Tests for the cheap new-bucket detector (``detect_new_buckets``).

The detector is the fast path that bounds the S3-born uncapped window. Its
contract:

- one ``ListBuckets`` call, then a targeted ``GetBucketInfo`` only for ids absent
  from the baseline (never for already-known buckets),
- the per-tick fan-out is BOUNDED: a create burst fetches at most
  ``MAX_TARGETED_BUCKET_READS`` this tick, the rest defer (re-detected next
  tick; the periodic walk backstops the whole burst),
- no baseline yet / unconfigured / ListBuckets failure all yield an empty list
  and touch nothing further.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from stormpulse.garage.config import GarageConfig
from stormpulse.garage.state import MAX_TARGETED_BUCKET_READS, detect_new_buckets
from tests.helpers import make_fake_garage_state, make_garage_bucket

ADMIN_URL = "http://127.0.0.1:3903"


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


def _state_with(ids: list[str]) -> Any:
    """A GarageState whose baseline contains exactly the given bucket ids."""
    return make_fake_garage_state().with_buckets([make_garage_bucket(i) for i in ids])


def _info(bucket_id: str) -> dict[str, Any]:
    return {
        "id": bucket_id,
        "globalAliases": [],
        "websiteAccess": False,
        "websiteConfig": None,
        "keys": [],
        "objects": 0,
        "bytes": 0,
        "quotas": {"maxSize": None, "maxObjects": None},
    }


@contextmanager
def _patched(listed_ids: list[str]) -> Iterator[dict[str, MagicMock]]:
    list_buckets = MagicMock(return_value=([{"id": i} for i in listed_ids], ""))
    get_info = MagicMock(
        side_effect=lambda *, admin_url, admin_token, bucket_ref: (_info(bucket_ref), "")
    )
    with (
        patch("stormpulse.garage.state.admin_api.list_buckets", list_buckets),
        patch("stormpulse.garage.state.admin_api.get_bucket_info", get_info),
    ):
        yield {"list_buckets": list_buckets, "get_info": get_info}


def test_newcomer_detected_and_only_it_is_fetched() -> None:
    baseline = _state_with(["known-a"])
    with _patched(["known-a", "new-b"]) as m:
        newcomers = detect_new_buckets(_config(), baseline)
    assert [b.id for b in newcomers] == ["new-b"]
    # Exactly one targeted fetch - the known bucket is never re-fetched.
    assert m["get_info"].call_count == 1
    assert m["list_buckets"].call_count == 1


def test_already_known_yields_nothing() -> None:
    baseline = _state_with(["known-a", "known-b"])
    with _patched(["known-a", "known-b"]) as m:
        newcomers = detect_new_buckets(_config(), baseline)
    assert newcomers == []
    assert m["get_info"].call_count == 0


def test_multiple_newcomers_in_one_diff_are_batched() -> None:
    baseline = _state_with(["known-a"])
    with _patched(["known-a", "new-b", "new-c"]) as m:
        newcomers = detect_new_buckets(_config(), baseline)
    assert {b.id for b in newcomers} == {"new-b", "new-c"}
    assert m["get_info"].call_count == 2


def test_fanout_is_capped_under_a_burst() -> None:
    baseline = _state_with(["known-a"])
    burst = [f"new-{i}" for i in range(MAX_TARGETED_BUCKET_READS + 5)]
    with _patched(["known-a", *burst]) as m:
        newcomers = detect_new_buckets(_config(), baseline)
    # Only the cap is fetched this tick; the overflow defers (re-detected next
    # tick, and the periodic walk backstops the whole burst).
    assert len(newcomers) == MAX_TARGETED_BUCKET_READS
    assert m["get_info"].call_count == MAX_TARGETED_BUCKET_READS


def test_no_baseline_yet_yields_nothing_without_admin_calls() -> None:
    with _patched(["new-b"]) as m:
        newcomers = detect_new_buckets(_config(), None)
    assert newcomers == []
    assert m["list_buckets"].call_count == 0


def test_unconfigured_yields_nothing() -> None:
    baseline = _state_with(["known-a"])
    with _patched(["known-a", "new-b"]) as m:
        newcomers = detect_new_buckets(_config(admin_url="", admin_token=""), baseline)
    assert newcomers == []
    assert m["list_buckets"].call_count == 0


def test_listbuckets_failure_yields_nothing() -> None:
    baseline = _state_with(["known-a"])
    failing = MagicMock(return_value=(None, "unreachable"))
    with patch("stormpulse.garage.state.admin_api.list_buckets", failing):
        newcomers = detect_new_buckets(_config(), baseline)
    assert newcomers == []
