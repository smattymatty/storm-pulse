"""Tests for stormpulse.garage.provision_bucket.

Provisions a bucket and its admin key atomically (5 steps). Additional
keys (rw/ro) are added later via ``provision_additional_key``.

Step ordering:

  1. bucket create <throwaway>
  2. key create <admin_name>
  3. bucket allow --read --write --owner <throwaway> --key <admin_id>
  4. bucket alias --local <admin_id> <throwaway> <display>
  5. bucket unalias <throwaway>

On any failure, atomic rollback runs in reverse order: detach local
alias, revoke permissions, delete key, delete bucket. Throwaway is
the bucket reference throughout — it's still attached during rollback
because step 5 is where it would have been removed.
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
# Fixtures
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
    assert len(bucket_uuid) == 16
    assert outcome.extras["admin"]["key_name"] == "key-admin"
    assert outcome.extras["admin"]["key_id"].startswith("GK")
    assert outcome.extras["admin"]["secret"]
    assert outcome.extras["step_completed"] == "unalias_throwaway"
    assert outcome.extras["step_failed"] is None
    assert outcome.extras["rollback_status"] == "not_required"
    assert outcome.extras["manual_cleanup_required"] == []
    # No rw / ro fields — those are separate orchestrator territory now
    assert "rw" not in outcome.extras
    assert "ro" not in outcome.extras


@pytest.mark.asyncio
async def test_happy_path_sequence_of_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, fake = await _run(monkeypatch)
    admin_key = outcome.extras["admin"]["key_id"]

    assert len(fake.calls) == 5
    # Step 1: bucket create <throwaway>
    assert fake.calls[0][:2] == ("bucket", "create")
    throwaway = fake.calls[0][2]
    assert throwaway.startswith("provisioning-")
    # Step 2: key create
    assert fake.calls[1] == ("key", "create", "key-admin")
    # Step 3: admin perm grant
    assert fake.calls[2] == (
        "bucket", "allow", "--read", "--write", "--owner",
        throwaway, "--key", admin_key,
    )
    # Step 4: admin local alias attach
    assert fake.calls[3] == (
        "bucket", "alias", "--local", admin_key, throwaway, "media",
    )
    # Step 5: unalias throwaway
    assert fake.calls[4] == ("bucket", "unalias", throwaway)


@pytest.mark.asyncio
async def test_happy_path_end_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After provisioning, the bucket has 0 globals + 1 local; the admin
    key has read+write+owner on the bucket.
    """
    outcome, fake = await _run(monkeypatch)
    bucket_uuid_short = outcome.extras["bucket_uuid"]
    admin_key = outcome.extras["admin"]["key_id"]

    assert len(fake.buckets) == 1
    bucket = next(iter(fake.buckets.values()))
    assert bucket.bucket_id.startswith(bucket_uuid_short)
    assert bucket.global_aliases == set()
    assert bucket.local_aliases == {admin_key: "media"}
    assert fake.keys[admin_key].permissions[bucket.bucket_id] == {
        "read", "write", "owner",
    }


# ---------------------------------------------------------------------------
# Failure-point tests — one per step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step1_bucket_create_failure_no_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGarage()
    fake.fail_next("bucket_create", stderr="cluster unreachable")
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
    """Step 2 fails: only bucket exists. Rollback: bucket delete."""
    fake = FakeGarage()
    fake.fail_next("key_create", stderr="key create error")
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.failure_reason == "admin_key_create_failed"
    assert outcome.extras["step_failed"] == "admin_key_create"
    assert outcome.extras["step_completed"] == "bucket_create"
    assert outcome.extras["rollback_status"] == "complete"
    throwaway = fake.calls[0][2]
    # Calls: create + key_create_fail + bucket_delete = 3
    assert len(fake.calls) == 3
    assert fake.calls[-1] == ("bucket", "delete", "--yes", throwaway)
    assert not fake.buckets
    assert not fake.keys


@pytest.mark.asyncio
async def test_step3_admin_perm_grant_failure_deletes_key_and_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 3 fails: bucket and admin key exist. Rollback: key delete, bucket delete."""
    fake = FakeGarage()
    fake.fail_next("bucket_allow", stderr="perm grant error")
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.failure_reason == "admin_permission_grant_failed"
    assert outcome.extras["step_failed"] == "admin_permission_grant"
    assert outcome.extras["step_completed"] == "admin_key_create"
    assert outcome.extras["rollback_status"] == "complete"
    throwaway = fake.calls[0][2]
    # Calls: create + key_create + perm_fail + key_delete + bucket_delete = 5
    assert len(fake.calls) == 5
    # No deny (no perms were granted), no unalias_local (no alias attached)
    assert all(c[:2] != ("bucket", "deny") for c in fake.calls)
    assert all(
        c[:3] != ("bucket", "unalias", "--local") for c in fake.calls
    )
    assert fake.calls[-2][:2] == ("key", "delete")
    assert fake.calls[-1] == ("bucket", "delete", "--yes", throwaway)
    assert not fake.buckets
    assert not fake.keys


@pytest.mark.asyncio
async def test_step4_admin_local_alias_failure_revokes_perms_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 4 fails: bucket, key, perms exist. Rollback: deny + key delete + bucket delete."""
    fake = FakeGarage()
    fake.fail_next("bucket_alias_local", stderr="alias attach error")
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.failure_reason == "admin_local_alias_attach_failed"
    assert outcome.extras["step_failed"] == "admin_local_alias_attach"
    assert outcome.extras["step_completed"] == "admin_permission_grant"
    assert outcome.extras["rollback_status"] == "complete"
    throwaway = fake.calls[0][2]
    deny_calls = [c for c in fake.calls if c[:2] == ("bucket", "deny")]
    assert len(deny_calls) == 1
    assert deny_calls[0][:5] == (
        "bucket", "deny", "--read", "--write", "--owner",
    )
    # No unalias_local (no alias attached)
    assert all(
        c[:3] != ("bucket", "unalias", "--local") for c in fake.calls
    )
    assert fake.calls[-1] == ("bucket", "delete", "--yes", throwaway)
    assert not fake.buckets
    assert not fake.keys


@pytest.mark.asyncio
async def test_step5_unalias_throwaway_failure_atomic_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 5 fails: everything exists. Atomic rollback: unalias + deny + key + bucket."""
    fake = FakeGarage()
    fake.fail_next("bucket_unalias", stderr="injected unalias failure")
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.failure_reason == "unalias_throwaway_failed"
    assert outcome.extras["step_failed"] == "unalias_throwaway"
    assert outcome.extras["step_completed"] == "admin_local_alias_attach"
    assert outcome.extras["rollback_status"] == "complete"
    unalias_local_calls = [
        c for c in fake.calls if c[:3] == ("bucket", "unalias", "--local")
    ]
    deny_calls = [c for c in fake.calls if c[:2] == ("bucket", "deny")]
    key_delete_calls = [c for c in fake.calls if c[:2] == ("key", "delete")]
    bucket_delete_calls = [
        c for c in fake.calls if c[:2] == ("bucket", "delete")
    ]
    assert len(unalias_local_calls) == 1
    # Confirm 1-positional after --local <key>
    assert len(unalias_local_calls[0]) == 5
    assert unalias_local_calls[0][4] == "media"
    assert len(deny_calls) == 1
    assert len(key_delete_calls) == 1
    assert len(bucket_delete_calls) == 1
    assert not fake.buckets
    assert not fake.keys


# ---------------------------------------------------------------------------
# Rollback-failure case
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_partial_when_cleanup_step_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 5 (final unalias) fails, then rollback's first step (unalias
    --local of admin) also fails. ``manual_cleanup_required`` lists
    every remaining artifact: the local alias, the permission grant,
    the key, and the bucket.
    """
    fake = FakeGarage()
    fake.fail_next("bucket_unalias", stderr="step 5 failure")
    fake.fail_next("bucket_unalias_local", stderr="rollback unalias failure")
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.failure_reason == "rollback_failed"
    assert outcome.extras["step_failed"] == "unalias_throwaway"
    assert outcome.extras["rollback_status"] == "partial"
    cleanup = outcome.extras["manual_cleanup_required"]
    types_ids = {(item["type"], item.get("key_id") or item.get("id"))
                 for item in cleanup}
    # Pull admin_id from the perm grant call (the trailing arg of bucket allow)
    allow_calls = [c for c in fake.calls if c[:2] == ("bucket", "allow")]
    assert len(allow_calls) == 1
    admin_id = allow_calls[0][-1]
    bucket_uuid_short = outcome.extras["bucket_uuid"]

    assert ("local_alias", admin_id) in types_ids
    assert ("permission_grant", admin_id) in types_ids
    assert ("key", admin_id) in types_ids
    assert ("bucket", bucket_uuid_short) in types_ids


# ---------------------------------------------------------------------------
# Handler factory
# ---------------------------------------------------------------------------


def test_handler_factory_returns_none_on_missing_params() -> None:
    handler = make_provision_customer_bucket_handler(
        _make_config(),
        params={"display_name": "media"},  # missing key_name_admin
    )
    assert handler is None


def test_handler_factory_returns_handler_when_complete() -> None:
    handler = make_provision_customer_bucket_handler(
        _make_config(),
        params={
            "display_name": "media",
            "key_name_admin": "k-a",
        },
    )
    assert handler is not None
    assert callable(handler)


def test_handler_factory_ignores_extra_legacy_params() -> None:
    """Storm-side may still pass key_name_rw / key_name_ro during the
    transition. The factory should ignore them rather than reject.
    """
    handler = make_provision_customer_bucket_handler(
        _make_config(),
        params={
            "display_name": "media",
            "key_name_admin": "k-a",
            "key_name_rw": "k-rw",  # legacy, ignored
            "key_name_ro": "k-ro",  # legacy, ignored
        },
    )
    assert handler is not None
