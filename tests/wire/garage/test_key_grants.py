"""Tiered keys and grant attach/detach against a real Garage.

``provision_additional_key`` mints an rw/ro key onto an existing bucket;
``attach``/``detach_account_key`` widen and narrow an account key's reach on a
bucket. All three end with a read-back that a fake can satisfy by construction,
so only a real Garage proves the grant that landed is the grant that was asked
for.

The wire-only claim that matters most here is **narrowing**: attach is a
precise SET, not just a widen. Re-attaching an owner grant as ``ro`` must strip
write and owner, which happens through ``DenyBucketKey`` on the complement and
is exactly the behavior a fake cannot vouch for. Verified against Garage
v2.3.0: denying write+owner on an owner grant leaves read-only.
"""

from __future__ import annotations

import pytest

from stormpulse.garage import admin_api
from stormpulse.garage.jobs.attach_account_key import run_attach_account_key
from stormpulse.garage.jobs.detach_account_key import run_detach_account_key
from stormpulse.garage.jobs.provision_account_key import run_provision_account_key
from stormpulse.garage.jobs.provision_additional_key import (
    run_provision_additional_key,
)
from stormpulse.garage.jobs.provision_bucket import run_provision_customer_bucket
from tests.wire.garage.conftest import WireEnv, pretty, unique_alias


class _ProgressRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, int, int | None, str]] = []

    async def __call__(
        self,
        stage: str,
        current: int,
        total: int | None,
        message: str,
        *,
        transfer: object | None = None,
        bytes_freed: object | None = None,
    ) -> None:
        self.events.append((stage, current, total, message))


async def _provisioned_bucket(wire: WireEnv) -> tuple[str, str, str]:
    """Provision a bucket + admin key. Returns (admin_key_id, short_id, full_id)."""
    outcome = await run_provision_customer_bucket(
        progress=_ProgressRecorder(),
        garage_config=wire.garage_config(),
        display_name=unique_alias("grant"),
        key_name_admin=unique_alias("adminkey"),
    )
    assert outcome.success, outcome.stderr
    short = outcome.extras["bucket_uuid"]
    info, err = admin_api.get_bucket_info(**wire.admin_kwargs, bucket_ref=short)
    assert err == "", err
    assert info is not None
    return outcome.extras["admin"]["key_id"], short, info["id"]


async def _account_key(wire: WireEnv) -> str:
    outcome = await run_provision_account_key(
        progress=_ProgressRecorder(),
        garage_config=wire.garage_config(),
        new_key_name=unique_alias("acctkey"),
        allow_create_bucket=False,
    )
    assert outcome.success, outcome.stderr
    return outcome.extras["new_key_id"]


def _perms_on(wire: WireEnv, key_id: str, full_id: str) -> dict[str, bool] | None:
    info, err = admin_api.get_key_info(**wire.admin_kwargs, access_key_id=key_id)
    assert err == "", err
    assert info is not None
    for entry in info.get("buckets") or []:
        if entry.get("id") == full_id:
            return entry.get("permissions")
    return None


def _reap(wire: WireEnv, key_id: str) -> None:
    admin_api.delete_key(**wire.admin_kwargs, access_key_id=key_id)


# ---------------------------------------------------------------------------
# provision_additional_key: the exact tier lands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tier,expected",
    [
        ("rw", {"read": True, "write": True, "owner": False}),
        ("ro", {"read": True, "write": False, "owner": False}),
    ],
)
@pytest.mark.asyncio
async def test_additional_key_lands_exactly_its_tier(
    wire: WireEnv, tier: str, expected: dict[str, bool]
) -> None:
    """An rw key is rw, an ro key is ro. Read back from Garage, not asserted at it.

    A fake echoes whatever tier was requested; a real read-back is the only
    proof the grant Garage recorded matches the tier. An ro key that quietly
    got write would let a read-only credential mutate customer data.
    """
    admin_key_id, short, full_id = await _provisioned_bucket(wire)

    outcome = await run_provision_additional_key(
        progress=_ProgressRecorder(),
        garage_config=wire.garage_config(),
        new_key_name=unique_alias(f"{tier}key"),
        bucket_id=short,
        local_alias=unique_alias("alias"),
        key_tier=tier,
    )
    assert outcome.success, outcome.stderr
    new_key_id = outcome.extras["new_key_id"]

    try:
        perms = _perms_on(wire, new_key_id, full_id)
        assert perms == expected, f"{tier} key landed {perms}, wanted {expected}"
    finally:
        admin_api.delete_bucket(**wire.admin_kwargs, bucket_ref=short)
        _reap(wire, admin_key_id)
        _reap(wire, new_key_id)


@pytest.mark.asyncio
async def test_additional_key_carries_its_one_time_secret(wire: WireEnv) -> None:
    """The new key's secret rides back once and is a usable 64-char credential."""
    admin_key_id, short, _full = await _provisioned_bucket(wire)
    outcome = await run_provision_additional_key(
        progress=_ProgressRecorder(),
        garage_config=wire.garage_config(),
        new_key_name=unique_alias("secretkey"),
        bucket_id=short,
        local_alias=unique_alias("alias"),
        key_tier="rw",
    )
    assert outcome.success, outcome.stderr
    new_key_id = outcome.extras["new_key_id"]
    try:
        assert len(outcome.extras["new_secret"]) == 64, "secret is not a full key"
    finally:
        admin_api.delete_bucket(**wire.admin_kwargs, bucket_ref=short)
        _reap(wire, admin_key_id)
        _reap(wire, new_key_id)


@pytest.mark.asyncio
async def test_additional_key_rolls_back_on_a_failed_grant(wire: WireEnv) -> None:
    """A grant against a nonexistent bucket fails, and the minted key is deleted.

    Reverse-order rollback for real: CreateKey succeeded, AllowBucketKey failed
    (the bucket ref resolves to nothing), and the created key must not survive
    as an untracked credential.
    """
    outcome = await run_provision_additional_key(
        progress=_ProgressRecorder(),
        garage_config=wire.garage_config(),
        new_key_name=unique_alias("doomedkey"),
        bucket_id="0" * 16,  # no such bucket
        local_alias=unique_alias("alias"),
        key_tier="rw",
    )
    assert not outcome.success
    assert outcome.extras["step_failed"] == "new_key_permission_grant", outcome.extras
    assert outcome.extras["rollback_status"] == "complete", (
        f"the minted key was not rolled back: {outcome.extras}"
    )
    new_key_id = outcome.extras["new_key_id"]
    assert new_key_id, outcome.extras

    # The rolled-back key is actually gone from Garage.
    info, err = admin_api.get_key_info(**wire.admin_kwargs, access_key_id=new_key_id)
    assert info is None, f"the rolled-back key {new_key_id} survived"
    assert admin_api.is_not_found(err), err


# ---------------------------------------------------------------------------
# attach: widen, and the precise-SET narrowing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tier,expected",
    [
        ("ro", {"read": True, "write": False, "owner": False}),
        ("rw", {"read": True, "write": True, "owner": False}),
        ("owner", {"read": True, "write": True, "owner": True}),
    ],
)
@pytest.mark.asyncio
async def test_attach_lands_exactly_the_tier(
    wire: WireEnv, tier: str, expected: dict[str, bool]
) -> None:
    """Each attach tier grants exactly its permission triple, read back."""
    admin_key_id, short, full_id = await _provisioned_bucket(wire)
    account_key_id = await _account_key(wire)

    try:
        outcome = await run_attach_account_key(
            progress=_ProgressRecorder(),
            garage_config=wire.garage_config(),
            bucket_id=short,
            account_key_id=account_key_id,
            local_alias=unique_alias("att"),
            tier=tier,
        )
        assert outcome.success, outcome.stderr
        assert _perms_on(wire, account_key_id, full_id) == expected
    finally:
        admin_api.delete_bucket(**wire.admin_kwargs, bucket_ref=short)
        _reap(wire, admin_key_id)
        _reap(wire, account_key_id)


@pytest.mark.asyncio
async def test_reattach_narrows_owner_down_to_read_only(wire: WireEnv) -> None:
    """Attach is a precise SET: owner re-attached as ro strips write and owner.

    The wire-only claim. AllowBucketKey only widens, so narrowing depends on
    the deny-the-complement step actually removing bits on the real server. If
    it did not, a scope reduction (owner -> ro) would silently leave the key
    with full access, the opposite of what the operator asked for and a
    least-privilege violation.
    """
    admin_key_id, short, full_id = await _provisioned_bucket(wire)
    account_key_id = await _account_key(wire)

    try:
        first = await run_attach_account_key(
            progress=_ProgressRecorder(),
            garage_config=wire.garage_config(),
            bucket_id=short, account_key_id=account_key_id,
            local_alias=unique_alias("att"), tier="owner",
        )
        assert first.success, first.stderr
        assert _perms_on(wire, account_key_id, full_id) == {
            "read": True, "write": True, "owner": True,
        }

        # Re-attach narrower. The deny-complement must strip write + owner.
        second = await run_attach_account_key(
            progress=_ProgressRecorder(),
            garage_config=wire.garage_config(),
            bucket_id=short, account_key_id=account_key_id,
            local_alias=unique_alias("att"), tier="ro",
        )
        assert second.success, second.stderr
        perms = _perms_on(wire, account_key_id, full_id)
        assert perms == {"read": True, "write": False, "owner": False}, (
            f"owner was not narrowed to ro; the precise-SET failed:\n{pretty(perms)}"
        )
    finally:
        admin_api.delete_bucket(**wire.admin_kwargs, bucket_ref=short)
        _reap(wire, admin_key_id)
        _reap(wire, account_key_id)


# ---------------------------------------------------------------------------
# detach: revoke the grant, keep the key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detach_removes_the_grant_but_not_the_key(wire: WireEnv) -> None:
    """Detach revokes the bucket grant and leaves the account key alive.

    Detach is grant-removal, never key-destruction: the key must survive with
    its other buckets. Here it is attached to two buckets and detached from
    one; the other grant must be untouched and the key must still exist.
    """
    admin_a, short_a, full_a = await _provisioned_bucket(wire)
    admin_b, short_b, full_b = await _provisioned_bucket(wire)
    account_key_id = await _account_key(wire)

    try:
        for short in (short_a, short_b):
            att = await run_attach_account_key(
                progress=_ProgressRecorder(),
                garage_config=wire.garage_config(),
                bucket_id=short, account_key_id=account_key_id,
                local_alias=unique_alias("att"), tier="rw",
            )
            assert att.success, att.stderr

        outcome = await run_detach_account_key(
            progress=_ProgressRecorder(),
            garage_config=wire.garage_config(),
            bucket_id=short_a,
            account_key_id=account_key_id,
            local_alias=unique_alias("att"),
        )
        assert outcome.success, outcome.stderr
        assert outcome.extras["confirmed_detached"] is True

        # Grant on A is gone; grant on B survives; the key still exists.
        assert _perms_on(wire, account_key_id, full_a) is None, (
            "the detached grant survived"
        )
        assert _perms_on(wire, account_key_id, full_b) == {
            "read": True, "write": True, "owner": False,
        }, "detach damaged an unrelated grant"
        info, err = admin_api.get_key_info(
            **wire.admin_kwargs, access_key_id=account_key_id
        )
        assert info is not None and err == "", "detach destroyed the key itself"
    finally:
        admin_api.delete_bucket(**wire.admin_kwargs, bucket_ref=short_a)
        admin_api.delete_bucket(**wire.admin_kwargs, bucket_ref=short_b)
        _reap(wire, admin_a)
        _reap(wire, admin_b)
        _reap(wire, account_key_id)


@pytest.mark.asyncio
async def test_attach_then_detach_round_trips_to_no_grant(wire: WireEnv) -> None:
    """The literal inverse: attach grants, detach removes, back to nothing."""
    admin_key_id, short, full_id = await _provisioned_bucket(wire)
    account_key_id = await _account_key(wire)

    try:
        alias = unique_alias("rt")
        att = await run_attach_account_key(
            progress=_ProgressRecorder(),
            garage_config=wire.garage_config(),
            bucket_id=short, account_key_id=account_key_id,
            local_alias=alias, tier="rw",
        )
        assert att.success, att.stderr
        assert _perms_on(wire, account_key_id, full_id) is not None

        det = await run_detach_account_key(
            progress=_ProgressRecorder(),
            garage_config=wire.garage_config(),
            bucket_id=short, account_key_id=account_key_id, local_alias=alias,
        )
        assert det.success, det.stderr
        assert _perms_on(wire, account_key_id, full_id) is None
    finally:
        admin_api.delete_bucket(**wire.admin_kwargs, bucket_ref=short)
        _reap(wire, admin_key_id)
        _reap(wire, account_key_id)
