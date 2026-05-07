"""Tests for stormpulse.garage.provision_bucket.

Covers the contract in
``_architecture/specs/cellar-bucket-naming-foundation.md`` (Issue 4):

- happy path: 11 steps, zero rollback, full success payload
- one test per failure point asserting the correct rollback ran
- atomic rollback at step 11 (the final step)
- one test where rollback itself fails partway and ``manual_cleanup_required``
  is correctly populated

Backed by ``FakeGarage`` (see ``tests/garage/_fake_garage.py``), a
stateful semantic fake that enforces real Garage rules.

Step ordering (post Change B):

  1. bucket create <throwaway>
  2-4. key create <name> × 3 (admin, rw, ro)
  5-7. bucket allow <flags> <throwaway> --key <kid> × 3
  8-10. bucket alias --local <kid> <throwaway> <display> × 3
  11. bucket unalias <throwaway>

The throwaway alias is the bucket's only globally-unique handle
during steps 1-10. Step 11 removes it; the 3 local aliases satisfy
Garage's orphan rule.

On any failure, atomic rollback runs in reverse order: detach local
aliases, revoke permissions, delete keys, delete bucket. The
throwaway is always still attached during rollback, so all rollback
CLI calls reference the bucket by ``throwaway_alias``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stormpulse.config import GarageConfig
from stormpulse.garage import provision_bucket
from stormpulse.garage.provision_bucket import (
    make_provision_customer_bucket_handler,
    run_provision_customer_bucket,
)
from tests.garage._fake_garage import FakeGarage

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_config() -> GarageConfig:
    return GarageConfig(
        enabled=True,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=Path("/opt/garage/garage.toml"),
        state_push_interval_seconds=300,
    )


class _ProgressRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, int, int | None, str]] = []

    async def __call__(
        self, stage: str, current: int, total: int | None, message: str,
    ) -> None:
        self.events.append((stage, current, total, message))


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeGarage | None = None,
) -> tuple[provision_bucket.JobOutcome, FakeGarage]:
    fake = fake or FakeGarage()
    monkeypatch.setattr(provision_bucket, "_run_garage", fake.run_garage)
    outcome = await run_provision_customer_bucket(
        progress=_ProgressRecorder(),
        garage_config=_make_config(),
        display_name="media",
        key_name_admin="key-admin",
        key_name_rw="key-rw",
        key_name_ro="key-ro",
    )
    return outcome, fake


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_full_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, _fake = await _run(monkeypatch)

    assert outcome.success is True
    assert outcome.exit_code == 0
    assert outcome.failure_reason is None
    bucket_uuid = outcome.extras["bucket_uuid"]
    assert bucket_uuid is not None
    # The exposed UUID is the 16-char prefix (Change A).
    assert len(bucket_uuid) == 16
    assert outcome.extras["admin"]["key_name"] == "key-admin"
    assert outcome.extras["rw"]["key_name"] == "key-rw"
    assert outcome.extras["ro"]["key_name"] == "key-ro"
    assert outcome.extras["step_completed"] == "unalias_throwaway"
    assert outcome.extras["step_failed"] is None
    assert outcome.extras["rollback_status"] == "not_required"
    assert outcome.extras["manual_cleanup_required"] == []


@pytest.mark.asyncio
async def test_happy_path_sequence_of_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 11 forward steps invoke garage with the right args, in order.

    All bucket references during steps 5-10 use the throwaway alias
    (Change A); the unalias of the throwaway is the last step
    (Change B).
    """
    outcome, fake = await _run(monkeypatch)
    admin_key = outcome.extras["admin"]["key_id"]
    rw_key = outcome.extras["rw"]["key_id"]
    ro_key = outcome.extras["ro"]["key_id"]

    assert len(fake.calls) == 11
    # Step 1: bucket create <throwaway>
    assert fake.calls[0][:2] == ("bucket", "create")
    throwaway = fake.calls[0][2]
    assert throwaway.startswith("provisioning-")
    # Steps 2-4: key creates
    assert fake.calls[1] == ("key", "create", "key-admin")
    assert fake.calls[2] == ("key", "create", "key-rw")
    assert fake.calls[3] == ("key", "create", "key-ro")
    # Steps 5-7: permission grants (bucket ref = throwaway)
    assert fake.calls[4] == (
        "bucket", "allow", "--read", "--write", "--owner",
        throwaway, "--key", admin_key,
    )
    assert fake.calls[5] == (
        "bucket", "allow", "--read", "--write",
        throwaway, "--key", rw_key,
    )
    assert fake.calls[6] == (
        "bucket", "allow", "--read",
        throwaway, "--key", ro_key,
    )
    # Steps 8-10: local alias attaches (bucket ref = throwaway)
    assert fake.calls[7] == (
        "bucket", "alias", "--local", admin_key, throwaway, "media",
    )
    assert fake.calls[8] == (
        "bucket", "alias", "--local", rw_key, throwaway, "media",
    )
    assert fake.calls[9] == (
        "bucket", "alias", "--local", ro_key, throwaway, "media",
    )
    # Step 11: unalias the throwaway
    assert fake.calls[10] == ("bucket", "unalias", throwaway)


@pytest.mark.asyncio
async def test_happy_path_end_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After provisioning, the bucket has 0 globals and 3 locals; each
    key has the granted permissions on the bucket.
    """
    outcome, fake = await _run(monkeypatch)
    bucket_uuid_short = outcome.extras["bucket_uuid"]

    assert len(fake.buckets) == 1
    bucket = next(iter(fake.buckets.values()))
    assert bucket.bucket_id.startswith(bucket_uuid_short)
    assert bucket.global_aliases == set()
    assert set(bucket.local_aliases.values()) == {"media"}
    assert len(bucket.local_aliases) == 3

    admin_key = outcome.extras["admin"]["key_id"]
    rw_key = outcome.extras["rw"]["key_id"]
    ro_key = outcome.extras["ro"]["key_id"]
    assert fake.keys[admin_key].permissions[bucket.bucket_id] == {
        "read", "write", "owner",
    }
    assert fake.keys[rw_key].permissions[bucket.bucket_id] == {
        "read", "write",
    }
    assert fake.keys[ro_key].permissions[bucket.bucket_id] == {"read"}


# ---------------------------------------------------------------------------
# Failure-point tests — one per step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step1_bucket_create_failure_no_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGarage()
    fake.fail_next(
        "bucket_create",
        stderr="garage create error: cluster unreachable",
    )
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.success is False
    assert outcome.failure_reason == "bucket_create_failed"
    assert outcome.extras["step_failed"] == "bucket_create"
    assert outcome.extras["step_completed"] is None
    assert outcome.extras["bucket_uuid"] is None
    assert outcome.extras["rollback_status"] == "not_required"
    assert outcome.extras["manual_cleanup_required"] == []
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_step2_admin_key_create_failure_deletes_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 2 fails: only the bucket exists; rollback deletes it."""
    fake = FakeGarage()
    fake.fail_next("key_create", stderr="key create error")
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.failure_reason == "key_create_failed"
    assert outcome.extras["step_failed"] == "key_create_admin"
    assert outcome.extras["step_index"] == 0
    assert outcome.extras["rollback_status"] == "complete"
    throwaway = fake.calls[0][2]
    # Calls: create, key_create (failed), bucket delete
    assert len(fake.calls) == 3
    assert fake.calls[-1] == ("bucket", "delete", "--yes", throwaway)
    # Bucket actually gone from fake state
    assert not fake.buckets


@pytest.mark.asyncio
async def test_step3_rw_key_create_failure_deletes_admin_key_and_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGarage()
    fake.fail_next("key_create", after=1, stderr="key create error")
    outcome, _ = await _run(monkeypatch, fake)
    admin_key = next(
        c[2] for c in fake.calls
        if c[:2] == ("key", "create") and c[2] == "key-admin"
    )
    # The actual admin key_id from fake state — captured BEFORE rollback
    # ran since the orchestrator records it in state.keys_created.
    # Fake.keys is empty after rollback; reconstruct from rollback calls.
    key_delete_calls = [c for c in fake.calls if c[:2] == ("key", "delete")]
    deleted_admin_id = key_delete_calls[0][3]
    del admin_key  # only used to confirm step 2 happened

    assert outcome.failure_reason == "key_create_failed"
    assert outcome.extras["step_failed"] == "key_create_rw"
    assert outcome.extras["step_index"] == 1
    assert outcome.extras["rollback_status"] == "complete"
    throwaway = fake.calls[0][2]
    # Calls: create, key_create_admin (OK), key_create_rw (fail), key_delete (admin), bucket_delete
    assert len(fake.calls) == 5
    assert fake.calls[-2] == ("key", "delete", "--yes", deleted_admin_id)
    assert fake.calls[-1] == ("bucket", "delete", "--yes", throwaway)
    assert not fake.buckets
    assert not fake.keys


@pytest.mark.asyncio
async def test_step4_ro_key_create_failure_deletes_two_keys_and_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGarage()
    fake.fail_next("key_create", after=2, stderr="key create error")
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.failure_reason == "key_create_failed"
    assert outcome.extras["step_failed"] == "key_create_ro"
    assert outcome.extras["step_index"] == 2
    assert outcome.extras["rollback_status"] == "complete"
    throwaway = fake.calls[0][2]
    # Calls: create, 2× key_create OK, 1× key_create fail, 2× key_delete, 1× bucket_delete
    assert len(fake.calls) == 7
    assert fake.calls[-1] == ("bucket", "delete", "--yes", throwaway)
    assert not fake.buckets
    assert not fake.keys


@pytest.mark.asyncio
async def test_step5_admin_perm_grant_failure_deletes_keys_and_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 5 (first perm grant) fails: 0 perms granted, 3 keys created.

    Rollback: skip the deny phase (nothing to revoke), delete 3 keys,
    delete bucket.
    """
    fake = FakeGarage()
    fake.fail_next("bucket_allow", stderr="perm grant error")
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.failure_reason == "permission_grant_failed"
    assert outcome.extras["step_failed"] == "permission_grant_admin"
    assert outcome.extras["step_index"] == 0
    assert outcome.extras["rollback_status"] == "complete"
    throwaway = fake.calls[0][2]
    # Calls: create + 3 key_create + 1 perm_grant_fail + 3 key_delete + 1 bucket_delete = 9
    assert len(fake.calls) == 9
    # No bucket_deny calls in rollback (no perms were granted)
    deny_calls = [c for c in fake.calls if c[:2] == ("bucket", "deny")]
    assert deny_calls == []
    assert fake.calls[-1] == ("bucket", "delete", "--yes", throwaway)
    assert not fake.buckets
    assert not fake.keys


@pytest.mark.asyncio
async def test_step6_rw_perm_grant_failure_revokes_admin_then_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 6 fails: admin's perms are granted, rw's grant fails.

    Rollback: 1 deny call (revoke admin), 3 key deletes, bucket delete.
    """
    fake = FakeGarage()
    fake.fail_next("bucket_allow", after=1, stderr="perm grant error")
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.failure_reason == "permission_grant_failed"
    assert outcome.extras["step_failed"] == "permission_grant_rw"
    assert outcome.extras["step_index"] == 1
    assert outcome.extras["rollback_status"] == "complete"
    deny_calls = [c for c in fake.calls if c[:2] == ("bucket", "deny")]
    assert len(deny_calls) == 1
    # Admin's deny revokes all three flags
    assert deny_calls[0][:5] == (
        "bucket", "deny", "--read", "--write", "--owner",
    )
    assert not fake.buckets
    assert not fake.keys


@pytest.mark.asyncio
async def test_step7_ro_perm_grant_failure_revokes_admin_and_rw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 7 fails: admin and rw perms granted, ro's grant fails.

    Rollback: 2 deny calls (admin then rw), 3 key deletes, bucket delete.
    """
    fake = FakeGarage()
    fake.fail_next("bucket_allow", after=2, stderr="perm grant error")
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.extras["step_failed"] == "permission_grant_ro"
    assert outcome.extras["step_index"] == 2
    assert outcome.extras["rollback_status"] == "complete"
    deny_calls = [c for c in fake.calls if c[:2] == ("bucket", "deny")]
    assert len(deny_calls) == 2
    # Admin first (RWO), rw second (RW)
    assert deny_calls[0][:5] == (
        "bucket", "deny", "--read", "--write", "--owner",
    )
    assert deny_calls[1][:4] == ("bucket", "deny", "--read", "--write")
    assert "--owner" not in deny_calls[1]
    assert not fake.buckets
    assert not fake.keys


@pytest.mark.asyncio
async def test_step8_admin_local_alias_failure_no_aliases_to_detach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 8 fails: 0 aliases attached. Rollback: 3 deny + 3 key_delete + bucket_delete."""
    fake = FakeGarage()
    fake.fail_next("bucket_alias_local", stderr="alias attach error")
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.failure_reason == "local_alias_attach_failed"
    assert outcome.extras["step_failed"] == "local_alias_attach_admin"
    assert outcome.extras["step_index"] == 0
    assert outcome.extras["rollback_status"] == "complete"
    unalias_calls = [
        c for c in fake.calls if c[:3] == ("bucket", "unalias", "--local")
    ]
    assert unalias_calls == []
    deny_calls = [c for c in fake.calls if c[:2] == ("bucket", "deny")]
    assert len(deny_calls) == 3
    assert not fake.buckets
    assert not fake.keys


@pytest.mark.asyncio
async def test_step9_rw_local_alias_failure_detaches_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 9 fails: admin alias attached, rw fails.

    Rollback: 1 unalias (admin), 3 deny, 3 key_delete, bucket_delete.
    """
    fake = FakeGarage()
    fake.fail_next("bucket_alias_local", after=1, stderr="alias attach error")
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.extras["step_failed"] == "local_alias_attach_rw"
    assert outcome.extras["step_index"] == 1
    assert outcome.extras["rollback_status"] == "complete"
    unalias_calls = [
        c for c in fake.calls if c[:3] == ("bucket", "unalias", "--local")
    ]
    assert len(unalias_calls) == 1
    # Confirm the rollback unalias is 1-positional (Change C)
    assert len(unalias_calls[0]) == 5  # ("bucket", "unalias", "--local", key, name)
    assert unalias_calls[0][4] == "media"
    deny_calls = [c for c in fake.calls if c[:2] == ("bucket", "deny")]
    assert len(deny_calls) == 3
    assert not fake.buckets
    assert not fake.keys


@pytest.mark.asyncio
async def test_step10_ro_local_alias_failure_detaches_admin_and_rw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 10 fails: admin + rw aliases attached, ro fails.

    Rollback: 2 unalias, 3 deny, 3 key_delete, bucket_delete.
    """
    fake = FakeGarage()
    fake.fail_next("bucket_alias_local", after=2, stderr="alias attach error")
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.extras["step_failed"] == "local_alias_attach_ro"
    assert outcome.extras["step_index"] == 2
    assert outcome.extras["rollback_status"] == "complete"
    unalias_calls = [
        c for c in fake.calls if c[:3] == ("bucket", "unalias", "--local")
    ]
    assert len(unalias_calls) == 2
    deny_calls = [c for c in fake.calls if c[:2] == ("bucket", "deny")]
    assert len(deny_calls) == 3
    assert not fake.buckets
    assert not fake.keys


@pytest.mark.asyncio
async def test_step11_unalias_throwaway_failure_atomic_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 11 (final unalias) fails: atomic rollback runs.

    All 3 aliases attached, all 3 perms granted, all 3 keys created.
    Rollback: 3 unalias, 3 deny, 3 key_delete, bucket_delete.

    Step 11 will not naturally fail (3 locals satisfy the orphan rule
    when the throwaway is removed), so this test injects the failure.
    """
    fake = FakeGarage()
    fake.fail_next("bucket_unalias", stderr="injected unalias failure")
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.failure_reason == "unalias_throwaway_failed"
    assert outcome.extras["step_failed"] == "unalias_throwaway"
    assert outcome.extras["step_completed"] == "local_alias_attach_ro"
    assert outcome.extras["rollback_status"] == "complete"
    unalias_local_calls = [
        c for c in fake.calls if c[:3] == ("bucket", "unalias", "--local")
    ]
    deny_calls = [c for c in fake.calls if c[:2] == ("bucket", "deny")]
    key_delete_calls = [c for c in fake.calls if c[:2] == ("key", "delete")]
    bucket_delete_calls = [
        c for c in fake.calls if c[:2] == ("bucket", "delete")
    ]
    assert len(unalias_local_calls) == 3
    assert len(deny_calls) == 3
    assert len(key_delete_calls) == 3
    assert len(bucket_delete_calls) == 1
    # Cluster is left clean
    assert not fake.buckets
    assert not fake.keys


# ---------------------------------------------------------------------------
# Rollback-failure case
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_partial_when_cleanup_step_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a rollback step itself fails, halt rollback and populate
    ``manual_cleanup_required``. The failure_reason flips to
    ``rollback_failed`` and the originating step stays in ``step_failed``.

    Scenario: step 10 fails (ro alias attach), then rollback's first
    step (unalias admin) also fails. Manual cleanup must include both
    still-attached aliases (admin, rw), all 3 permissions, all 3 keys,
    and the bucket.
    """
    fake = FakeGarage()
    fake.fail_next("bucket_alias_local", after=2, stderr="alias attach error")
    fake.fail_next("bucket_unalias_local", stderr="unalias error during rollback")
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.failure_reason == "rollback_failed"
    assert outcome.extras["step_failed"] == "local_alias_attach_ro"
    assert outcome.extras["rollback_status"] == "partial"
    cleanup = outcome.extras["manual_cleanup_required"]
    types_ids = {(item["type"], item.get("key_id") or item.get("id"))
                 for item in cleanup}
    # Pull the actual key IDs from the bucket allow calls (each
    # ends with "--key <kid>").
    allow_calls = [c for c in fake.calls if c[:2] == ("bucket", "allow")]
    assert len(allow_calls) == 3
    admin_id = allow_calls[0][-1]
    rw_id = allow_calls[1][-1]
    ro_id = allow_calls[2][-1]

    assert ("local_alias", admin_id) in types_ids
    assert ("local_alias", rw_id) in types_ids
    assert ("local_alias", ro_id) not in types_ids  # ro never attached
    assert ("permission_grant", admin_id) in types_ids
    assert ("permission_grant", rw_id) in types_ids
    assert ("permission_grant", ro_id) in types_ids
    assert ("key", admin_id) in types_ids
    assert ("key", rw_id) in types_ids
    assert ("key", ro_id) in types_ids
    bucket_uuid_short = outcome.extras["bucket_uuid"]
    assert ("bucket", bucket_uuid_short) in types_ids


# ---------------------------------------------------------------------------
# Handler factory
# ---------------------------------------------------------------------------


def test_handler_factory_returns_none_on_missing_params() -> None:
    handler = make_provision_customer_bucket_handler(
        _make_config(),
        params={"display_name": "media"},
    )
    assert handler is None


def test_handler_factory_returns_handler_when_complete() -> None:
    handler = make_provision_customer_bucket_handler(
        _make_config(),
        params={
            "display_name": "media",
            "key_name_admin": "k-a",
            "key_name_rw": "k-rw",
            "key_name_ro": "k-ro",
        },
    )
    assert handler is not None
    assert callable(handler)
