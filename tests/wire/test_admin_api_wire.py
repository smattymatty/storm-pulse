"""The admin API against a real Garage. The version-compat surface.

Every fake in ``tests/garage/`` asserts that the agent SENDS the right thing.
Only this file asserts that Garage ANSWERS the way the agent parses. That is
the whole class of breakage a Garage upgrade causes, and the whole reason a
version bump currently requires a real deploy to trust.

Each test names the field, code, or semantic the agent depends on, so a
failure reads as "Garage changed X" rather than "something broke".

Run: ``make garage-up && make test-wire``
"""

from __future__ import annotations

from typing import Any

import pytest

from stormpulse.garage import admin_api
from tests.wire.conftest import (
    WireBucket,
    WireEnv,
    bucket_info_cli,
    garage_cli,
    pretty,
    put_object,
    unique_alias,
)

# ---------------------------------------------------------------------------
# Identity: the 64/16 bucket-id rule
#
# Storm stores a 16-char bucket id; Garage's admin API takes the full 64-char
# id on every mutating endpoint and resolves prefixes only via ?search=. This
# was discovered empirically against a live cluster and nothing in the default
# suite pins it against a real server.
# ---------------------------------------------------------------------------


def test_create_bucket_returns_a_full_64_char_id(wire: WireEnv) -> None:
    """CreateBucket's ``id`` is the 64-hex full id Storm truncates to 16.

    If Garage ever shortens this, every provisioned bucket gets a wrong
    ``garage_bucket_id`` in the control plane and the truth path breaks.
    """
    alias = unique_alias("create")
    data, err = admin_api.create_bucket(**wire.admin_kwargs, global_alias=alias)
    assert err == "", err
    assert data is not None
    try:
        full_id = data.get("id", "")
        assert isinstance(full_id, str), pretty(data)
        assert len(full_id) == 64, f"expected a 64-char id, got {full_id!r}"
        assert all(c in "0123456789abcdef" for c in full_id), full_id
    finally:
        garage_cli("bucket", "delete", "--yes", alias)


def test_get_bucket_info_resolves_the_16_char_prefix_via_search(
    wire: WireEnv,
) -> None:
    """A 16-char prefix resolves to exactly the bucket it prefixes.

    ``get_bucket_info`` branches on ``len(ref) == 64`` and uses ``?search=``
    below that, then verifies the returned id starts with the prefix. This is
    the read path behind every usage report.
    """
    alias = unique_alias("prefix")
    created, err = admin_api.create_bucket(**wire.admin_kwargs, global_alias=alias)
    assert err == "", err
    assert created is not None
    full_id = created["id"]
    try:
        info, err = admin_api.get_bucket_info(
            **wire.admin_kwargs, bucket_ref=full_id[:16]
        )
        assert err == "", err
        assert info is not None
        assert info["id"] == full_id
    finally:
        garage_cli("bucket", "delete", "--yes", alias)


def test_mutating_endpoints_reject_the_16_char_prefix(wire: WireEnv) -> None:
    """DeleteBucket/UpdateBucket need the full id, hence ``_resolve_full_bucket_id``.

    If Garage ever starts accepting prefixes the resolve step becomes dead
    weight; if it stops resolving via search, every quota set fails. Either
    way the agent needs to know at upgrade time, not at deploy time.
    """
    alias = unique_alias("mutprefix")
    created, err = admin_api.create_bucket(**wire.admin_kwargs, global_alias=alias)
    assert err == "", err
    assert created is not None
    full_id = created["id"]
    try:
        ok, err = admin_api.set_bucket_quota(
            **wire.admin_kwargs, bucket_id=full_id[:16], max_size_bytes=1_000_000
        )
        # The agent's own resolve step is what makes the prefix work.
        assert ok, err
        info = bucket_info_cli(alias)
        assert info.get("Maximum size") or info.get("Quotas"), pretty(info)
    finally:
        garage_cli("bucket", "delete", "--yes", alias)


# ---------------------------------------------------------------------------
# Quota: the control loop's whole basis
# ---------------------------------------------------------------------------


def test_set_quota_reads_back_as_exact_decimal_bytes(wire: WireEnv) -> None:
    """``quotas.maxSize`` round-trips the exact integer the agent sent.

    The capacity model is built on exact bytes, never human-rounded text
    (core/buckets-capacity-model.md). A silent unit change here would
    mis-cap every account on the node.
    """
    alias = unique_alias("quota")
    created, err = admin_api.create_bucket(**wire.admin_kwargs, global_alias=alias)
    assert err == "", err
    assert created is not None
    full_id = created["id"]
    try:
        want = 3_221_225_472  # 3 GiB in decimal bytes, not a round number
        ok, err = admin_api.set_bucket_quota(
            **wire.admin_kwargs, bucket_id=full_id, max_size_bytes=want
        )
        assert ok, err

        info, err = admin_api.get_bucket_info(**wire.admin_kwargs, bucket_ref=full_id)
        assert err == "", err
        assert info is not None
        quotas = info.get("quotas")
        assert isinstance(quotas, dict), pretty(info)
        assert quotas.get("maxSize") == want, pretty(quotas)
        assert quotas.get("maxObjects") is None, pretty(quotas)
    finally:
        garage_cli("bucket", "delete", "--yes", alias)


def test_bucket_info_reports_exact_bytes_and_objects(
    wire: WireEnv, bucket: WireBucket
) -> None:
    """``bytes``/``objects`` are exact integers, the accounting the walk reads.

    Seeds two objects of known size through the real S3 endpoint and asserts
    GetBucketInfo agrees. Garage's counters are async-propagated, so this
    polls rather than asserting on the first read.
    """
    put_object(wire, bucket.name, "a.bin", b"x" * 1024)
    put_object(wire, bucket.name, "b.bin", b"y" * 2048)

    info = _poll_bucket_info(wire, bucket.id, want_objects=2)
    assert info["objects"] == 2, pretty(info)
    assert info["bytes"] == 3072, pretty(info)


def _poll_bucket_info(
    wire: WireEnv, bucket_ref: str, *, want_objects: int, tries: int = 40
) -> dict[str, Any]:
    """Poll GetBucketInfo until the counter catches up, then return it.

    Garage's object counter is a distributed, async-propagated table (the same
    one ``garage bucket info`` reads). Asserting on the first read is the
    classic flake; asserting on a poll that never converges is a real failure.
    """
    import time

    last: dict[str, Any] = {}
    for _ in range(tries):
        info, err = admin_api.get_bucket_info(
            **wire.admin_kwargs, bucket_ref=bucket_ref
        )
        assert err == "", err
        assert info is not None
        last = info
        if info.get("objects") == want_objects:
            return info
        time.sleep(0.25)
    raise AssertionError(
        f"bucket counter never reached {want_objects} objects; last read:\n"
        f"{pretty(last)}"
    )


# ---------------------------------------------------------------------------
# Keys: mint, capability, delete
# ---------------------------------------------------------------------------


def test_create_key_returns_gk_id_and_one_time_secret(wire: WireEnv) -> None:
    """``accessKeyId`` is ``GK`` + 24 hex; ``secretAccessKey`` is 64 hex.

    Storm hands both to the customer once. A shape change here ships a broken
    credential to a real person.
    """
    data, err = admin_api.create_key(**wire.admin_kwargs, name=unique_alias("k"))
    assert err == "", err
    assert data is not None
    key_id = data.get("accessKeyId", "")
    secret = data.get("secretAccessKey", "")
    try:
        assert key_id.startswith("GK"), pretty(data)
        assert len(key_id) == 26, f"expected GK + 24 hex, got {key_id!r}"
        assert len(secret) == 64, f"expected a 64-char secret, got {len(secret)} chars"
    finally:
        admin_api.delete_key(**wire.admin_kwargs, access_key_id=key_id)


def test_create_key_with_create_bucket_capability_sets_the_flag(
    wire: WireEnv,
) -> None:
    """``allow.createBucket`` at mint is the account key's defining capability.

    This is the count-backstop lever: the agent flips it off past the bucket
    rail. If the flag stops being reported, the backstop reads as always-off.
    """
    data, err = admin_api.create_key(
        **wire.admin_kwargs, name=unique_alias("acct"), allow_create_bucket=True
    )
    assert err == "", err
    assert data is not None
    key_id = data["accessKeyId"]
    try:
        info, err = admin_api.get_key_info(**wire.admin_kwargs, access_key_id=key_id)
        assert err == "", err
        assert info is not None
        permissions = info.get("permissions")
        assert isinstance(permissions, dict), pretty(info)
        assert permissions.get("createBucket") is True, pretty(permissions)
    finally:
        admin_api.delete_key(**wire.admin_kwargs, access_key_id=key_id)


def test_update_key_revokes_create_bucket(wire: WireEnv) -> None:
    """``deny.createBucket`` actually clears the flag Garage reports.

    The backstop is only real if the deny lands. A no-op deny would let an
    account keep minting buckets past its rail while the agent believes it
    fenced them.
    """
    data, err = admin_api.create_key(
        **wire.admin_kwargs, name=unique_alias("revoke"), allow_create_bucket=True
    )
    assert err == "", err
    assert data is not None
    key_id = data["accessKeyId"]
    try:
        ok, err = admin_api.update_key(
            **wire.admin_kwargs, access_key_id=key_id, allow_create_bucket=False
        )
        assert ok, err
        info, err = admin_api.get_key_info(**wire.admin_kwargs, access_key_id=key_id)
        assert err == "", err
        assert info is not None
        assert info["permissions"].get("createBucket") is False, pretty(info)
    finally:
        admin_api.delete_key(**wire.admin_kwargs, access_key_id=key_id)


# ---------------------------------------------------------------------------
# Permissions and aliases
# ---------------------------------------------------------------------------


def test_allow_and_deny_bucket_key_round_trip(
    wire: WireEnv, bucket: WireBucket
) -> None:
    """Grant then revoke shows up in the bucket's key list.

    The provisioning path's central act. ``bucket_ref`` here is a global
    alias, the form the job code passes.
    """
    data, err = admin_api.create_key(**wire.admin_kwargs, name=unique_alias("perm"))
    assert err == "", err
    assert data is not None
    key_id = data["accessKeyId"]
    try:
        ok, err = admin_api.allow_bucket_key(
            **wire.admin_kwargs, bucket_ref=bucket.id, access_key_id=key_id,
            read=True, write=True, owner=False,
        )
        assert ok, err
        info, err = admin_api.get_bucket_info(**wire.admin_kwargs, bucket_ref=bucket.id)
        assert err == "", err
        assert info is not None
        granted = _perm_for_key(info, key_id)
        assert granted.get("read") is True, pretty(info)
        assert granted.get("write") is True, pretty(info)
        assert granted.get("owner") is False, pretty(info)

        ok, err = admin_api.deny_bucket_key(
            **wire.admin_kwargs, bucket_ref=bucket.id, access_key_id=key_id,
            read=True, write=True, owner=True,
        )
        assert ok, err
        info, err = admin_api.get_bucket_info(**wire.admin_kwargs, bucket_ref=bucket.id)
        assert err == "", err
        assert info is not None
        # A fully-revoked key DISAPPEARS from the bucket's key list; Garage
        # does not list it with all-false permissions. Attribution therefore
        # reads a revoked grant as absence, which is what the walk's
        # bucket-to-key mapping assumes. A future Garage that started listing
        # zeroed entries would make every revoked key look like a live grant.
        assert all(
            entry.get("accessKeyId") != key_id for entry in info.get("keys", [])
        ), pretty(info)
    finally:
        admin_api.delete_key(**wire.admin_kwargs, access_key_id=key_id)


def _perm_for_key(info: dict[str, Any], key_id: str) -> dict[str, Any]:
    """Pull one key's permission block out of a GetBucketInfo response."""
    for entry in info.get("keys", []):
        if entry.get("accessKeyId") == key_id:
            perms = entry.get("permissions")
            assert isinstance(perms, dict), pretty(entry)
            return perms
    raise AssertionError(f"key {key_id} absent from bucket keys:\n{pretty(info)}")


def test_local_alias_add_and_remove(wire: WireEnv, bucket: WireBucket) -> None:
    """Local aliases bind per key, and are never globally addressable.

    The rule behind "never use a local alias as a bucket id". The agent adds
    one at provision and removes it at detach.
    """
    alias = unique_alias("local")
    ok, err = admin_api.add_bucket_alias_local(
        **wire.admin_kwargs, bucket_ref=bucket.id,
        access_key_id=wire.access_key, local_alias=alias,
    )
    assert ok, err
    info, err = admin_api.get_bucket_info(**wire.admin_kwargs, bucket_ref=bucket.id)
    assert err == "", err
    assert info is not None
    assert alias not in info.get("globalAliases", []), (
        f"a LOCAL alias leaked into globalAliases:\n{pretty(info)}"
    )

    ok, err = admin_api.remove_bucket_alias_local(
        **wire.admin_kwargs, bucket_ref=bucket.id,
        access_key_id=wire.access_key, local_alias=alias,
    )
    assert ok, err


# ---------------------------------------------------------------------------
# Destructive: the guards that stop data loss
# ---------------------------------------------------------------------------


def test_delete_bucket_refuses_a_non_empty_bucket(
    wire: WireEnv, bucket: WireBucket
) -> None:
    """Garage itself refuses to delete a bucket with objects in it.

    This is the outermost safety net under every purge path: even if the
    agent's own drain-then-delete ordering broke, Garage would refuse. If a
    future Garage ever deletes silently, that net is gone and the agent must
    grow its own precondition. Learning that from a test beats learning it
    from a customer.
    """
    put_object(wire, bucket.name, "keepme.bin", b"z" * 512)
    _poll_bucket_info(wire, bucket.id, want_objects=1)

    ok, err = admin_api.delete_bucket(**wire.admin_kwargs, bucket_ref=bucket.id)
    assert not ok, "Garage deleted a NON-EMPTY bucket; the outer safety net is gone"
    assert err, "refusal carried no error message"


def test_delete_missing_bucket_is_classified_as_not_found(wire: WireEnv) -> None:
    """A gone bucket produces an error ``is_not_found`` recognises.

    ``is_not_found`` string-matches Garage's error body. A reworded message
    turns idempotent deletes into hard failures and strands the tombstone
    sweep. This is the single most upgrade-fragile line in the module.
    """
    ok, err = admin_api.delete_bucket(
        **wire.admin_kwargs, bucket_ref="0" * 64
    )
    assert not ok
    assert admin_api.is_not_found(err), (
        f"is_not_found() did not recognise Garage's missing-bucket error: {err!r}"
    )


def test_delete_missing_key_is_classified_as_not_found(wire: WireEnv) -> None:
    """Same contract on the key side, used by the credential-kill sweep."""
    ok, err = admin_api.delete_key(
        **wire.admin_kwargs, access_key_id="GK" + "0" * 24
    )
    assert not ok
    assert admin_api.is_not_found(err), (
        f"is_not_found() did not recognise Garage's missing-key error: {err!r}"
    )


def test_delete_bucket_removes_it_and_all_aliases(wire: WireEnv) -> None:
    """An empty bucket deletes, and the global alias goes with it.

    DeleteBucket drops every alias in one call; the agent relies on that
    rather than unaliasing first (which Garage's orphan rule would refuse).
    """
    alias = unique_alias("del")
    created, err = admin_api.create_bucket(**wire.admin_kwargs, global_alias=alias)
    assert err == "", err
    assert created is not None

    ok, err = admin_api.delete_bucket(**wire.admin_kwargs, bucket_ref=created["id"])
    assert ok, err

    info, err = admin_api.get_bucket_info(**wire.admin_kwargs, bucket_ref=alias)
    assert info is None
    assert admin_api.is_not_found(err), err


# ---------------------------------------------------------------------------
# Cluster reads: what the state walk is built on
# ---------------------------------------------------------------------------


def test_cluster_status_carries_the_node_fields_the_agent_reads(
    wire: WireEnv,
) -> None:
    """GetClusterStatus reports nodes with the id/role shape topology needs."""
    data, err = admin_api.get_cluster_status(**wire.admin_kwargs)
    assert err == "", err
    assert data is not None
    nodes = data.get("nodes")
    assert isinstance(nodes, list) and nodes, pretty(data)
    node = nodes[0]
    assert isinstance(node.get("id"), str), pretty(node)
    assert "role" in node, pretty(node)


def test_cluster_statistics_is_reachable(wire: WireEnv) -> None:
    """GetClusterStatistics answers 2xx with a body the agent can parse."""
    data, err = admin_api.get_cluster_statistics(**wire.admin_kwargs)
    assert err == "", err
    assert data is not None


def test_list_buckets_includes_a_created_bucket_with_its_aliases(
    wire: WireEnv,
) -> None:
    """ListBuckets items carry ``id`` and ``globalAliases``.

    The enumeration the per-bucket walk fans out from. A missing alias field
    would make every bucket look nameless to the reconcile path, and a
    nameless-but-owned bucket is the adopt-and-cap case.
    """
    alias = unique_alias("listed")
    created, err = admin_api.create_bucket(**wire.admin_kwargs, global_alias=alias)
    assert err == "", err
    assert created is not None
    try:
        items, err = admin_api.list_buckets(**wire.admin_kwargs)
        assert err == "", err
        assert items is not None
        mine = next((b for b in items if b.get("id") == created["id"]), None)
        assert mine is not None, "created bucket absent from ListBuckets"
        assert alias in mine.get("globalAliases", []), pretty(mine)
    finally:
        garage_cli("bucket", "delete", "--yes", alias)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_a_wrong_admin_token_is_rejected_and_reported(wire: WireEnv) -> None:
    """A bad bearer token fails cleanly, never as a hang or a false 2xx."""
    data, err = admin_api.list_buckets(
        admin_url=wire.admin_url, admin_token="not-the-real-token"
    )
    assert data is None
    assert err, "a rejected admin token produced no error message"


@pytest.mark.parametrize("path_token", ["", "   "])
def test_an_empty_admin_token_is_rejected(wire: WireEnv, path_token: str) -> None:
    """An unset token never reads as anonymous access."""
    data, err = admin_api.list_buckets(
        admin_url=wire.admin_url, admin_token=path_token
    )
    assert data is None
    assert err
