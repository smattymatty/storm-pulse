"""Provisioning and key rotation against a real Garage.

These are the orchestrations customer accounts are built on: mint a bucket and
its admin key, mint an account key at a tier, rotate a key by transferring
every grant to its replacement, and leak-rotate a compromised key by
snapshotting then reaping it. Every one is a multi-step flow whose correctness
depends on what Garage actually does with the calls, and the fakes can only
prove the agent sends the right sequence, not that Garage answers the way the
convergence reads.

Two claims here are wire-only and load-bearing:

- **Atomicity.** ``provision_bucket`` makes a single ``CreateBucket`` call bind
  the admin key's local alias AND its read/write/owner grant. A fake asserts
  the call shape; only Garage proves the alias and the perms both landed.
- **The tier backstop.** An account key minted without the createBucket
  capability must be REFUSED an S3 CreateBucket. That refusal happens at
  Garage's S3 endpoint, nowhere the agent can see, so nothing but the wire
  proves the tier is real rather than merely recorded.
"""

from __future__ import annotations

from typing import Any

import pytest

from stormpulse.garage import admin_api
from stormpulse.garage.jobs.converge_account_key_rotation import (
    run_converge_account_key_rotation,
)
from stormpulse.garage.jobs.provision_account_key import run_provision_account_key
from stormpulse.garage.jobs.provision_bucket import run_provision_customer_bucket
from stormpulse.garage.jobs.snapshot_and_reap_account_key import (
    run_snapshot_and_reap_account_key,
)
from tests.wire.garage.conftest import (
    WireEnv,
    delete_bucket_cli,
    garage_cli,
    pretty,
    s3_create_bucket,
    unique_alias,
)


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


def _key_grant(
    wire: WireEnv, key_id: str, bucket_full_id: str
) -> dict[str, Any] | None:
    """The key's permission entry for one bucket, read back from GetKeyInfo."""
    info, err = admin_api.get_key_info(**wire.admin_kwargs, access_key_id=key_id)
    assert err == "", err
    assert info is not None
    for entry in info.get("buckets") or []:
        if entry.get("id") == bucket_full_id:
            return entry
    return None


def _reap_key(wire: WireEnv, key_id: str) -> None:
    admin_api.delete_key(**wire.admin_kwargs, access_key_id=key_id)


# ---------------------------------------------------------------------------
# provision_bucket: the atomic bucket + admin key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provision_binds_the_admin_alias_and_perms_in_one_call(
    wire: WireEnv,
) -> None:
    """The single CreateBucket call left the admin key owning the bucket, aliased.

    This is the atomicity claim. After the job, the minted admin key must hold
    read+write+owner on the new bucket AND carry the display_name as its local
    alias, both from the one Garage transaction.
    """
    display = unique_alias("acct")
    key_name = unique_alias("adminkey")
    progress = _ProgressRecorder()

    outcome = await run_provision_customer_bucket(
        progress=progress,
        garage_config=wire.garage_config(),
        display_name=display,
        key_name_admin=key_name,
    )

    assert outcome.success, outcome.stderr
    admin_key_id = outcome.extras["admin"]["key_id"]
    bucket_short = outcome.extras["bucket_uuid"]
    assert len(bucket_short) == 16, bucket_short

    try:
        # Resolve the full id, then read the grant the admin key holds on it.
        info, err = admin_api.get_bucket_info(
            **wire.admin_kwargs, bucket_ref=bucket_short
        )
        assert err == "", err
        assert info is not None
        full_id = info["id"]

        grant = _key_grant(wire, admin_key_id, full_id)
        assert grant is not None, "the admin key does not own the bucket it provisioned"
        assert grant["permissions"] == {"read": True, "write": True, "owner": True}, (
            pretty(grant)
        )
        assert display in grant.get("localAliases", []), (
            f"the display name did not land as the admin key's local alias:\n"
            f"{pretty(grant)}"
        )
    finally:
        admin_api.delete_bucket(**wire.admin_kwargs, bucket_ref=bucket_short)
        _reap_key(wire, admin_key_id)


@pytest.mark.asyncio
async def test_provision_rolls_back_the_orphan_key_when_the_bucket_fails(
    wire: WireEnv,
) -> None:
    """A failed CreateBucket leaves NO key behind. Rollback, proven for real.

    Garage rejects a local alias with spaces (400), so a display_name like
    "Bad Name" fails the CreateBucket step after CreateKey already succeeded.
    The orphan-key rollback must then delete that key: a leaked admin key holds
    nothing yet, but it is a credential nobody is tracking.
    """
    key_name = unique_alias("orphan")
    progress = _ProgressRecorder()

    outcome = await run_provision_customer_bucket(
        progress=progress,
        garage_config=wire.garage_config(),
        display_name="Bad Name With Spaces",  # Garage refuses this local alias
        key_name_admin=key_name,
    )

    assert not outcome.success, "expected the bad display name to fail CreateBucket"
    assert outcome.extras["step_failed"] == "bucket_create", outcome.extras
    assert outcome.extras["rollback_status"] == "complete", (
        f"the orphan admin key was not rolled back: {outcome.extras}"
    )

    # No key by that name survives. (Garage lists by name; the rollback deleted
    # by id, so an absent name is the end-to-end proof.)
    listing = garage_cli("key", "list")
    assert key_name not in listing.stdout, (
        f"a key named {key_name} survived a rolled-back provision:\n{listing.stdout}"
    )


# ---------------------------------------------------------------------------
# provision_account_key: the tier gate, and the backstop it buys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_account_key_with_create_can_make_a_bucket_over_s3(
    wire: WireEnv,
) -> None:
    """An Admin-tier account key mints with createBucket, and it actually works.

    The job sets the flag; the flag has to translate into an allowed S3
    CreateBucket, which only the real endpoint decides.
    """
    progress = _ProgressRecorder()
    outcome = await run_provision_account_key(
        progress=progress,
        garage_config=wire.garage_config(),
        new_key_name=unique_alias("admin-tier"),
        allow_create_bucket=True,
    )
    assert outcome.success, outcome.stderr
    key_id = outcome.extras["new_key_id"]
    secret = outcome.extras["new_secret"]
    made = f"made-{key_id[2:10].lower()}"

    try:
        status = s3_create_bucket(wire, key_id, secret, made)
        assert status == 200, f"a create-tier key was refused CreateBucket: {status}"
    finally:
        delete_bucket_cli(made)
        _reap_key(wire, key_id)


@pytest.mark.asyncio
async def test_account_key_without_create_is_refused_a_bucket_over_s3(
    wire: WireEnv,
) -> None:
    """The backstop: a Read-tier key is REFUSED an S3 CreateBucket (403).

    The security claim the whole tier system rests on. If a no-create key could
    still make buckets, the count-backstop and the tier are decorative. Nothing
    but the S3 endpoint proves the refusal, so nothing but a wire test can.
    """
    progress = _ProgressRecorder()
    outcome = await run_provision_account_key(
        progress=progress,
        garage_config=wire.garage_config(),
        new_key_name=unique_alias("read-tier"),
        allow_create_bucket=False,
    )
    assert outcome.success, outcome.stderr
    key_id = outcome.extras["new_key_id"]
    secret = outcome.extras["new_secret"]

    try:
        status = s3_create_bucket(wire, key_id, secret, f"forbidden-{key_id[2:8].lower()}")
        assert status == 403, (
            f"a no-create account key was allowed to create a bucket "
            f"(HTTP {status}); the tier backstop is not real"
        )
    finally:
        # If Garage regressed and the bucket WAS made, clean it up.
        delete_bucket_cli(f"forbidden-{key_id[2:8].lower()}")
        _reap_key(wire, key_id)


@pytest.mark.asyncio
async def test_account_key_tier_flag_reads_back_from_getkeyinfo(
    wire: WireEnv,
) -> None:
    """The flag the job set is the flag Garage reports. The internal side of the pair."""
    progress = _ProgressRecorder()
    outcome = await run_provision_account_key(
        progress=progress,
        garage_config=wire.garage_config(),
        new_key_name=unique_alias("tierflag"),
        allow_create_bucket=False,
    )
    assert outcome.success, outcome.stderr
    key_id = outcome.extras["new_key_id"]
    try:
        info, err = admin_api.get_key_info(**wire.admin_kwargs, access_key_id=key_id)
        assert err == "", err
        assert info is not None
        assert info["permissions"].get("createBucket") is False, pretty(info)
    finally:
        _reap_key(wire, key_id)


# ---------------------------------------------------------------------------
# converge_account_key_rotation: transfer every grant to the replacement
# ---------------------------------------------------------------------------


async def _provision_owned_bucket(wire: WireEnv) -> tuple[str, str, str]:
    """Provision a bucket + admin key. Returns (admin_key_id, bucket_short, full_id)."""
    outcome = await run_provision_customer_bucket(
        progress=_ProgressRecorder(),
        garage_config=wire.garage_config(),
        display_name=unique_alias("rot"),
        key_name_admin=unique_alias("rotkey"),
    )
    assert outcome.success, outcome.stderr
    admin_key_id = outcome.extras["admin"]["key_id"]
    bucket_short = outcome.extras["bucket_uuid"]
    info, err = admin_api.get_bucket_info(**wire.admin_kwargs, bucket_ref=bucket_short)
    assert err == "", err
    assert info is not None
    return admin_key_id, bucket_short, info["id"]


@pytest.mark.asyncio
async def test_rotation_transfers_ownership_to_the_new_key(wire: WireEnv) -> None:
    """One pass grants the new key everything the old key owned, then converges.

    The convergence reads the old key's ``buckets`` array from GetKeyInfo,
    grants each to the new key, and a second pass that finds nothing left is
    the converged signal.
    """
    old_key_id, bucket_short, full_id = await _provision_owned_bucket(wire)
    new = await run_provision_account_key(
        progress=_ProgressRecorder(),
        garage_config=wire.garage_config(),
        new_key_name=unique_alias("newkey"),
        allow_create_bucket=True,
    )
    new_key_id = new.extras["new_key_id"]

    try:
        first = await run_converge_account_key_rotation(
            progress=_ProgressRecorder(),
            garage_config=wire.garage_config(),
            old_key_id=old_key_id,
            new_key_id=new_key_id,
        )
        assert first.success, first.stderr
        assert first.extras["converged"] is False
        assert bucket_short in first.extras["transferred"], first.extras

        # The new key now owns the bucket, at the old key's tier.
        grant = _key_grant(wire, new_key_id, full_id)
        assert grant is not None, "the new key did not receive the bucket"
        assert grant["permissions"] == {"read": True, "write": True, "owner": True}

        # Second pass finds nothing to move: converged.
        second = await run_converge_account_key_rotation(
            progress=_ProgressRecorder(),
            garage_config=wire.garage_config(),
            old_key_id=old_key_id,
            new_key_id=new_key_id,
        )
        assert second.extras["converged"] is True, second.extras
        assert second.extras["transferred"] == []
    finally:
        admin_api.delete_bucket(**wire.admin_kwargs, bucket_ref=bucket_short)
        _reap_key(wire, old_key_id)
        _reap_key(wire, new_key_id)


@pytest.mark.asyncio
async def test_rotation_preserves_a_read_only_tier(wire: WireEnv) -> None:
    """A read-only grant transfers as read-only, not silently promoted to owner.

    The tier-aware transfer. An account with an rw/ro attach must not have it
    escalated to owner by a rotation. This is the property ``_owned`` /
    ``_covers`` exist to hold, checked against real Garage grants.
    """
    old_key_id, bucket_short, full_id = await _provision_owned_bucket(wire)

    # Mint a second key and grant it READ-ONLY on the same bucket, then rotate
    # THAT key. Its single grant is read-only and must arrive as read-only.
    ro = await run_provision_account_key(
        progress=_ProgressRecorder(),
        garage_config=wire.garage_config(),
        new_key_name=unique_alias("ro-old"),
        allow_create_bucket=False,
    )
    ro_key_id = ro.extras["new_key_id"]
    ok, err = admin_api.allow_bucket_key(
        **wire.admin_kwargs, bucket_ref=full_id, access_key_id=ro_key_id,
        read=True, write=False, owner=False,
    )
    assert ok, err

    new = await run_provision_account_key(
        progress=_ProgressRecorder(),
        garage_config=wire.garage_config(),
        new_key_name=unique_alias("ro-new"),
        allow_create_bucket=False,
    )
    new_key_id = new.extras["new_key_id"]

    try:
        outcome = await run_converge_account_key_rotation(
            progress=_ProgressRecorder(),
            garage_config=wire.garage_config(),
            old_key_id=ro_key_id,
            new_key_id=new_key_id,
        )
        assert outcome.success, outcome.stderr

        grant = _key_grant(wire, new_key_id, full_id)
        assert grant is not None, "the read-only grant did not transfer"
        assert grant["permissions"] == {
            "read": True, "write": False, "owner": False,
        }, f"a read-only tier was escalated on rotation:\n{pretty(grant)}"
    finally:
        admin_api.delete_bucket(**wire.admin_kwargs, bucket_ref=bucket_short)
        _reap_key(wire, old_key_id)
        _reap_key(wire, ro_key_id)
        _reap_key(wire, new_key_id)


@pytest.mark.asyncio
async def test_rotation_of_an_already_gone_old_key_is_converged(
    wire: WireEnv,
) -> None:
    """A 404 on the old key means nothing to transfer: converged, not failed.

    The self-heal must not wedge on an old key that was already reaped.
    """
    new = await run_provision_account_key(
        progress=_ProgressRecorder(),
        garage_config=wire.garage_config(),
        new_key_name=unique_alias("survivor"),
        allow_create_bucket=True,
    )
    new_key_id = new.extras["new_key_id"]
    try:
        outcome = await run_converge_account_key_rotation(
            progress=_ProgressRecorder(),
            garage_config=wire.garage_config(),
            old_key_id="GK" + "0" * 24,  # never existed
            new_key_id=new_key_id,
        )
        assert outcome.success, outcome.stderr
        assert outcome.extras["converged"] is True, outcome.extras
    finally:
        _reap_key(wire, new_key_id)


# ---------------------------------------------------------------------------
# The leak-rotate: snapshot, reap, then converge from the snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leak_rotate_snapshots_reaps_then_transfers_from_the_snapshot(
    wire: WireEnv,
) -> None:
    """The whole compromised-key flow, end to end against Garage.

    Snapshot the old key's owned buckets, delete the key object (so its lost
    secret can never be used again), then converge the new key from the
    captured snapshot rather than from the now-dead key. The new key must end
    up owning what the old key owned.
    """
    old_key_id, bucket_short, full_id = await _provision_owned_bucket(wire)
    new = await run_provision_account_key(
        progress=_ProgressRecorder(),
        garage_config=wire.garage_config(),
        new_key_name=unique_alias("leaknew"),
        allow_create_bucket=True,
    )
    new_key_id = new.extras["new_key_id"]

    try:
        # 1+2: snapshot then reap the old key.
        reap = await run_snapshot_and_reap_account_key(
            progress=_ProgressRecorder(),
            garage_config=wire.garage_config(),
            old_key_id=old_key_id,
        )
        assert reap.success, reap.stderr
        snapshot = reap.extras["snapshot"]
        assert any(e["id"] == full_id for e in snapshot), (
            f"the owned bucket was not captured before the reap:\n{pretty(snapshot)}"
        )

        # The old key is gone now.
        gone, err = admin_api.get_key_info(
            **wire.admin_kwargs, access_key_id=old_key_id
        )
        assert gone is None and admin_api.is_not_found(err), err

        # 3: converge the new key from the snapshot (the old key is unreadable).
        converge = await run_converge_account_key_rotation(
            progress=_ProgressRecorder(),
            garage_config=wire.garage_config(),
            old_key_id=old_key_id,
            new_key_id=new_key_id,
            bucket_snapshot=[
                {"id": e["id"], "alias": e.get("alias", ""), "perms": e["perms"]}
                for e in snapshot
            ],
        )
        assert converge.success, converge.stderr

        grant = _key_grant(wire, new_key_id, full_id)
        assert grant is not None, "the new key did not inherit the leaked key's bucket"
        assert grant["permissions"]["owner"] is True, pretty(grant)
    finally:
        admin_api.delete_bucket(**wire.admin_kwargs, bucket_ref=bucket_short)
        _reap_key(wire, new_key_id)


@pytest.mark.asyncio
async def test_reap_of_an_already_gone_key_is_an_idempotent_success(
    wire: WireEnv,
) -> None:
    """Reaping a key that is already gone succeeds with an empty snapshot.

    A retried leak-rotate must not fail on the second run.
    """
    outcome = await run_snapshot_and_reap_account_key(
        progress=_ProgressRecorder(),
        garage_config=wire.garage_config(),
        old_key_id="GK" + "0" * 24,
    )
    assert outcome.success, outcome.stderr
    assert outcome.extras["snapshot"] == []
