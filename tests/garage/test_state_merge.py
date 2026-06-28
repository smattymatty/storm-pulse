"""Tests for the shared GarageState merge primitive (``with_buckets`` / ``with_bucket``).

This is the single merge path every targeted writer uses (the new-bucket
detector and the post-mutation hook). The invariants it must hold:

- upsert by id: an incoming bucket replaces the same-id entry in place, a new
  id is appended, and unaffected buckets keep their position;
- full-snapshot-never-partial: the result always carries every prior bucket
  plus the merged one(s), because the control plane reads ``buckets`` as a
  manifest and a partial would read as deletions (manifest alarms, never acts);
- immutability: the source state is frozen and untouched; the result is a new
  object.
"""

from __future__ import annotations

from stormpulse.garage.state import GarageBucket, GarageState
from tests.helpers import make_garage_bucket

ID_A = "aaaa000000000000" + "0" * 48
ID_B = "bbbb000000000000" + "0" * 48
ID_C = "cccc000000000000" + "0" * 48


def _state(buckets: list[GarageBucket]) -> GarageState:
    return GarageState(
        node_id="n1",
        hostname="h",
        zone="z",
        capacity_gb=1.0,
        data_avail_gb=1.0,
        version="v",
        healthy=True,
        object_count=0,
        buckets=buckets,
        keys=[],
        peers=[],
    )


def _ids(state: GarageState) -> list[str]:
    return [b.id for b in state.buckets]


def test_upsert_replaces_in_place_and_preserves_position() -> None:
    state = _state([make_garage_bucket(ID_A), make_garage_bucket(ID_B, size_bytes=10), make_garage_bucket(ID_C)])
    merged = state.with_buckets([make_garage_bucket(ID_B, size_bytes=999)])
    # B is replaced where it sat; A and C keep their slots.
    assert _ids(merged) == [ID_A, ID_B, ID_C]
    by_id = {b.id: b for b in merged.buckets}
    assert by_id[ID_B].size_bytes == 999


def test_new_bucket_is_appended() -> None:
    state = _state([make_garage_bucket(ID_A), make_garage_bucket(ID_B)])
    merged = state.with_buckets([make_garage_bucket(ID_C, size_bytes=5)])
    assert _ids(merged) == [ID_A, ID_B, ID_C]


def test_merge_many_mixes_upsert_and_append() -> None:
    state = _state([make_garage_bucket(ID_A), make_garage_bucket(ID_B, size_bytes=1)])
    merged = state.with_buckets([make_garage_bucket(ID_B, size_bytes=2), make_garage_bucket(ID_C)])
    assert _ids(merged) == [ID_A, ID_B, ID_C]
    assert {b.id: b for b in merged.buckets}[ID_B].size_bytes == 2


def test_full_snapshot_never_partial() -> None:
    # Merging ONE bucket into a 3-bucket state must yield all 3, never just the
    # merged one - a partial reads downstream as two deletions.
    state = _state([make_garage_bucket(ID_A), make_garage_bucket(ID_B), make_garage_bucket(ID_C)])
    merged = state.with_buckets([make_garage_bucket(ID_B, size_bytes=42)])
    assert len(merged.buckets) == 3
    assert set(_ids(merged)) == {ID_A, ID_B, ID_C}


def test_empty_merge_returns_full_set() -> None:
    state = _state([make_garage_bucket(ID_A), make_garage_bucket(ID_B)])
    assert _ids(state.with_buckets([])) == [ID_A, ID_B]


def test_falsy_id_bucket_ignored() -> None:
    state = _state([make_garage_bucket(ID_A)])
    merged = state.with_buckets([make_garage_bucket(""), make_garage_bucket(ID_B)])
    # The blank-id bucket never enters the manifest; the real newcomer does.
    assert _ids(merged) == [ID_A, ID_B]


def test_source_state_is_untouched() -> None:
    original_buckets = [make_garage_bucket(ID_A, size_bytes=1)]
    state = _state(original_buckets)
    merged = state.with_buckets([make_garage_bucket(ID_A, size_bytes=2)])
    assert merged is not state
    # The original list and its bucket are unchanged (frozen state, new object).
    assert state.buckets[0].size_bytes == 1
    assert merged.buckets[0].size_bytes == 2
