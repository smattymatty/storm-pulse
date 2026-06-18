"""Tests for BucketIdResolver (ADR BUCKETS-015).

The resolver turns a GarageState snapshot into a frozen
``(key_id, name) -> bucket_id`` lookup. The invariant it must hold: a name
that resolves resolves to exactly one bucket, and an unknown name (or a
no-bucket line) resolves to ``''`` so the website falls back to key-anchoring.
"""

from __future__ import annotations

from stormpulse.garage.state import GarageBucket, GarageKeyRef, GarageState
from stormpulse.garage.bucket_resolver import BucketIdResolver


def _bucket(
    bucket_id: str,
    *,
    alias: str = "",
    keys: list[GarageKeyRef] | None = None,
) -> GarageBucket:
    return GarageBucket(
        id=bucket_id,
        alias=alias,
        size_bytes=0,
        object_count=0,
        keys=keys or [],
        website_access=False,
        website_index_document="index.html",
        website_error_document=None,
        quota_max_size_bytes=None,
        quota_max_objects=None,
    )


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


def _keyref(key_id: str, aliases: tuple[str, ...]) -> GarageKeyRef:
    return GarageKeyRef(
        key_id=key_id,
        key_name="k",
        permissions="RWO",
        bucket_local_aliases=aliases,
    )


def test_key_scoped_local_alias_resolves() -> None:
    state = _state([
        _bucket("bid-media-0001", keys=[_keyref("GKaccount", ("media",))]),
    ])
    resolver = BucketIdResolver.from_state(state)
    assert resolver.resolve("GKaccount", "media") == "bid-media-0001"


def test_global_alias_fallback_resolves() -> None:
    # Dashboard-provisioned bucket: carries a global alias, owning key holds
    # no local alias.
    state = _state([_bucket("bid-dash-0001", alias="dash-bucket")])
    resolver = BucketIdResolver.from_state(state)
    assert resolver.resolve("GKwhatever", "dash-bucket") == "bid-dash-0001"


def test_same_name_two_buckets_disambiguated_by_key() -> None:
    """Two of one account's buckets share the display name 'media' as a
    local alias under different keys. Each (key, name) must land its own id,
    never the sibling's. This is the within-account misfile BUCKETS-015 makes
    structurally impossible.
    """
    state = _state([
        _bucket("bid-first-0001", keys=[_keyref("GKkeyA", ("media",))]),
        _bucket("bid-second-002", keys=[_keyref("GKkeyB", ("media",))]),
    ])
    resolver = BucketIdResolver.from_state(state)
    assert resolver.resolve("GKkeyA", "media") == "bid-first-0001"
    assert resolver.resolve("GKkeyB", "media") == "bid-second-002"


def test_key_scoped_wins_over_global_alias() -> None:
    # A name that exists as both a key-local alias (-> A) and a global alias
    # (-> B): the key-scoped, more specific match wins.
    state = _state([
        _bucket("bid-A-00000001", keys=[_keyref("GKkey", ("shared",))]),
        _bucket("bid-B-00000002", alias="shared"),
    ])
    resolver = BucketIdResolver.from_state(state)
    assert resolver.resolve("GKkey", "shared") == "bid-A-00000001"
    # A different key only sees the global alias.
    assert resolver.resolve("GKother", "shared") == "bid-B-00000002"


def test_unknown_name_resolves_empty() -> None:
    state = _state([_bucket("bid-known-0001", alias="known")])
    resolver = BucketIdResolver.from_state(state)
    assert resolver.resolve("GKkey", "brand-new-bucket") == ""


def test_empty_name_resolves_empty() -> None:
    # Admin-operation lines and bucket-less requests carry no name.
    resolver = BucketIdResolver.from_state(_state([_bucket("bid-x-00000001")]))
    assert resolver.resolve("", "") == ""
    assert resolver.resolve("GKkey", "") == ""


def test_none_state_yields_empty_resolver() -> None:
    # Garage not live / no snapshot yet: every lookup is '' (fall back to
    # key-anchoring website-side).
    resolver = BucketIdResolver.from_state(None)
    assert resolver.resolve("GKkey", "anything") == ""


def test_bucket_with_blank_id_skipped() -> None:
    state = _state([_bucket("", alias="nameless")])
    resolver = BucketIdResolver.from_state(state)
    assert resolver.resolve("GKkey", "nameless") == ""


def test_callable_matches_resolve() -> None:
    state = _state([_bucket("bid-c-00000001", keys=[_keyref("GKkey", ("c",))])])
    resolver = BucketIdResolver.from_state(state)
    assert resolver("GKkey", "c") == resolver.resolve("GKkey", "c") == "bid-c-00000001"
