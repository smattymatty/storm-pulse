"""Unit tests for ``stormpulse.garage.state.affected_bucket_ids``.

The read-planning half of the post-mutation targeted re-read: given a command's
validated params plus the current snapshot, which bucket ids should be re-read.
Precedence, not per-command branching (see the function docstring).
"""

from __future__ import annotations

from stormpulse.garage.state import (
    GarageBucket,
    GarageKeyRef,
    GarageState,
    affected_bucket_ids,
)
from tests.helpers import make_fake_garage_state, make_garage_bucket

_ID_A = "a" * 64
_ID_B = "b" * 64
_ID_C = "c" * 64
_KEY = "GKkey"


def _owned_by(bucket_id: str, key_id: str) -> GarageBucket:
    return make_garage_bucket(bucket_id, keys=[GarageKeyRef(key_id, "k", "RWO")])


def _state_with(*buckets: GarageBucket) -> GarageState:
    return make_fake_garage_state().with_items(buckets)


def test_bucket_id_wins_over_key() -> None:
    """A named bucket_id is the target; a key param present is only the grantee."""
    state = _state_with(_owned_by(_ID_B, _KEY), _owned_by(_ID_C, _KEY))
    assert affected_bucket_ids({"bucket_id": _ID_A, "key_id": _KEY}, state) == [_ID_A]


def test_alias_only_suppresses_key_path() -> None:
    """A bucket named by alias defers to the walk and must NOT fan out the key's set."""
    state = _state_with(_owned_by(_ID_B, _KEY), _owned_by(_ID_C, _KEY))
    assert affected_bucket_ids({"bucket_name": "foo", "key_id": _KEY}, state) == []


def test_key_only_filters_the_keys_buckets() -> None:
    """No bucket param: the buckets that key touches, by in-memory grant filter."""
    state = _state_with(
        make_garage_bucket(_ID_A),  # no keys: untouched
        _owned_by(_ID_B, _KEY),
        _owned_by(_ID_C, _KEY),
    )
    assert set(affected_bucket_ids({"key_id": _KEY}, state)) == {_ID_B, _ID_C}


def test_returns_empty_when_nothing_named() -> None:
    state = _state_with(_owned_by(_ID_B, _KEY))
    assert affected_bucket_ids({}, state) == []
    assert affected_bucket_ids({"key_id": "GKabsent"}, state) == []
