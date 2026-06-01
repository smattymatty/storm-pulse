"""Tests for stormpulse.garage.provision_additional_key.

Adds a new tiered key (rw or ro) to an existing bucket. Three steps
with atomic rollback. The bucket itself is never touched by rollback.

Step ordering:

  1. key create <new_key_name>
  2. bucket allow <tier-flags> <bucket_id> --key <new_key_id>
  3. bucket alias --local <new_key_id> <bucket_id> <local_alias>
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stormpulse.commands.jobs import JobOutcome
from stormpulse.config import GarageConfig
from stormpulse.garage import provision_additional_key
from stormpulse.garage.provision_additional_key import (
    make_provision_additional_key_handler,
    run_provision_additional_key,
)
from tests.garage._fake_garage import FakeGarage


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
        self,
        stage: str,
        current: int,
        total: int | None,
        message: str,
    ) -> None:
        self.events.append((stage, current, total, message))


def _setup_fake_with_bucket() -> tuple[FakeGarage, str]:
    """Create a fake with an existing bucket. Returns (fake, bucket_id_16char)."""
    fake = FakeGarage()
    bucket = fake.add_bucket("media")
    return fake, bucket.bucket_id[:16]


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeGarage,
    bucket_id: str,
    *,
    new_key_name: str = "new-rw-key",
    local_alias: str = "media",
    key_tier: str = "rw",
) -> JobOutcome:
    monkeypatch.setattr(
        provision_additional_key,
        "run_garage",
        fake.run_garage,
    )
    return await run_provision_additional_key(
        progress=_ProgressRecorder(),
        garage_config=_make_config(),
        new_key_name=new_key_name,
        bucket_id=bucket_id,
        local_alias=local_alias,
        key_tier=key_tier,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_rw_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, bucket_id = _setup_fake_with_bucket()

    outcome = await _run(monkeypatch, fake, bucket_id, key_tier="rw")

    assert outcome.success is True
    assert outcome.failure_reason is None
    new_key_id = outcome.extras["new_key_id"]
    assert new_key_id is not None
    assert new_key_id.startswith("GK")
    assert outcome.extras["new_secret"]
    assert outcome.extras["new_key_name"] == "new-rw-key"
    assert outcome.extras["key_tier"] == "rw"
    assert outcome.extras["step_completed"] == "new_key_alias_attach"
    assert outcome.extras["rollback_status"] == "not_required"

    assert len(fake.calls) == 3
    assert fake.calls[0] == ("key", "create", "new-rw-key")
    assert fake.calls[1] == (
        "bucket",
        "allow",
        "--read",
        "--write",
        bucket_id,
        "--key",
        new_key_id,
    )
    assert fake.calls[2] == (
        "bucket",
        "alias",
        "--local",
        new_key_id,
        bucket_id,
        "media",
    )

    # End state: new key has rw perms; bucket has new local alias
    bucket = next(iter(fake.buckets.values()))
    assert fake.keys[new_key_id].permissions[bucket.bucket_id] == {
        "read",
        "write",
    }
    assert bucket.local_aliases[new_key_id] == "media"


@pytest.mark.asyncio
async def test_happy_path_ro_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    fake, bucket_id = _setup_fake_with_bucket()

    outcome = await _run(
        monkeypatch,
        fake,
        bucket_id,
        new_key_name="new-ro-key",
        key_tier="ro",
    )

    assert outcome.success is True
    new_key_id = outcome.extras["new_key_id"]
    assert outcome.extras["key_tier"] == "ro"
    # ro grants only --read
    assert fake.calls[1] == (
        "bucket",
        "allow",
        "--read",
        bucket_id,
        "--key",
        new_key_id,
    )
    assert "--write" not in fake.calls[1]
    assert "--owner" not in fake.calls[1]
    bucket = next(iter(fake.buckets.values()))
    assert fake.keys[new_key_id].permissions[bucket.bucket_id] == {"read"}


# ---------------------------------------------------------------------------
# Failure points
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step1_key_create_failure_no_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, bucket_id = _setup_fake_with_bucket()
    fake.fail_next("key_create", stderr="key create error")

    outcome = await _run(monkeypatch, fake, bucket_id)

    assert outcome.success is False
    assert outcome.failure_reason == "new_key_create_failed"
    assert outcome.extras["step_failed"] == "new_key_create"
    assert outcome.extras["new_key_id"] is None
    assert outcome.extras["rollback_status"] == "not_required"
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_step2_perm_grant_failure_deletes_new_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, bucket_id = _setup_fake_with_bucket()
    fake.fail_next("bucket_allow", stderr="perm grant error")

    outcome = await _run(monkeypatch, fake, bucket_id)
    new_key_id = outcome.extras["new_key_id"]

    assert outcome.failure_reason == "new_key_permission_grant_failed"
    assert outcome.extras["step_failed"] == "new_key_permission_grant"
    assert outcome.extras["rollback_status"] == "complete"
    # 3 calls: create key + fail allow + delete key
    assert len(fake.calls) == 3
    assert fake.calls[-1] == ("key", "delete", "--yes", new_key_id)
    # No deny calls (no perms granted)
    assert all(c[:2] != ("bucket", "deny") for c in fake.calls)
    # Bucket untouched
    assert len(fake.buckets) == 1
    # New key gone
    assert new_key_id not in fake.keys


@pytest.mark.asyncio
async def test_step3_alias_attach_failure_revokes_and_deletes_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, bucket_id = _setup_fake_with_bucket()
    fake.fail_next("bucket_alias_local", stderr="alias attach error")

    outcome = await _run(monkeypatch, fake, bucket_id)
    new_key_id = outcome.extras["new_key_id"]

    assert outcome.failure_reason == "new_key_alias_attach_failed"
    assert outcome.extras["step_failed"] == "new_key_alias_attach"
    assert outcome.extras["rollback_status"] == "complete"
    deny_calls = [c for c in fake.calls if c[:2] == ("bucket", "deny")]
    assert len(deny_calls) == 1
    # rw tier denies --read --write
    assert deny_calls[0] == (
        "bucket",
        "deny",
        "--read",
        "--write",
        bucket_id,
        "--key",
        new_key_id,
    )
    assert fake.calls[-1] == ("key", "delete", "--yes", new_key_id)
    # No unalias_local (alias never attached)
    assert all(c[:3] != ("bucket", "unalias", "--local") for c in fake.calls)
    # Bucket still alive
    assert len(fake.buckets) == 1
    assert new_key_id not in fake.keys


# ---------------------------------------------------------------------------
# Rollback-failure cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_partial_when_perm_revoke_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 3 fails; rollback's deny call also fails."""
    fake, bucket_id = _setup_fake_with_bucket()
    fake.fail_next("bucket_alias_local", stderr="alias attach error")
    fake.fail_next("bucket_deny", stderr="deny error during rollback")

    outcome = await _run(monkeypatch, fake, bucket_id)
    new_key_id = outcome.extras["new_key_id"]

    assert outcome.failure_reason == "rollback_failed"
    assert outcome.extras["step_failed"] == "new_key_alias_attach"
    assert outcome.extras["rollback_status"] == "partial"
    cleanup = outcome.extras["manual_cleanup_required"]
    types_ids = {
        (item["type"], item.get("key_id") or item.get("id")) for item in cleanup
    }
    assert ("permission_grant", new_key_id) in types_ids
    assert ("key", new_key_id) in types_ids
    # Bucket itself never appears in cleanup - this orchestrator doesn't own it
    assert not any(t == "bucket" for t, _ in types_ids)


@pytest.mark.asyncio
async def test_rollback_partial_when_key_delete_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 2 fails; rollback's key delete also fails."""
    fake, bucket_id = _setup_fake_with_bucket()
    fake.fail_next("bucket_allow", stderr="perm grant error")
    fake.fail_next("key_delete", stderr="key delete error during rollback")

    outcome = await _run(monkeypatch, fake, bucket_id)
    new_key_id = outcome.extras["new_key_id"]

    assert outcome.failure_reason == "rollback_failed"
    assert outcome.extras["step_failed"] == "new_key_permission_grant"
    assert outcome.extras["rollback_status"] == "partial"
    cleanup = outcome.extras["manual_cleanup_required"]
    types_ids = {
        (item["type"], item.get("key_id") or item.get("id")) for item in cleanup
    }
    assert ("key", new_key_id) in types_ids
    # No permission_grant entry - perms were never granted
    assert not any(t == "permission_grant" for t, _ in types_ids)


# ---------------------------------------------------------------------------
# Bucket reference: the orchestrator passes through whatever the caller
# provided. Garage CLI accepts global alias or 16-char prefix; the fake
# rejects 64-char hashes. If Storm ever passed a 64-char form, the fake
# would surface it as a NoSuchBucket failure at step 2.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_64_char_bucket_ref_rejected_by_garage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the caller supplies a 64-char bucket UUID, Garage CLI (and the
    fake) reject it. The orchestrator surfaces this as a step 2
    permission grant failure with NoSuchBucket in stderr.
    """
    fake = FakeGarage()
    bucket = fake.add_bucket("media")
    full_uuid = bucket.bucket_id  # 64 chars

    outcome = await _run(monkeypatch, fake, full_uuid)

    assert outcome.failure_reason == "new_key_permission_grant_failed"
    assert "NoSuchBucket" in outcome.stderr
    # New key was created and then rolled back
    new_key_id = outcome.extras["new_key_id"]
    assert outcome.extras["rollback_status"] == "complete"
    assert new_key_id not in fake.keys


# ---------------------------------------------------------------------------
# Handler factory
# ---------------------------------------------------------------------------


def test_handler_factory_returns_none_on_missing_params() -> None:
    handler = make_provision_additional_key_handler(
        _make_config(),
        params={"new_key_name": "new-key"},
    )
    assert handler is None


def test_handler_factory_returns_none_on_missing_tier() -> None:
    handler = make_provision_additional_key_handler(
        _make_config(),
        params={
            "new_key_name": "new-key",
            "bucket_id": "abcd1234abcd1234",
            "local_alias": "media",
            # key_tier missing
        },
    )
    assert handler is None


def test_handler_factory_returns_none_on_invalid_tier() -> None:
    handler = make_provision_additional_key_handler(
        _make_config(),
        params={
            "new_key_name": "new-key",
            "bucket_id": "abcd1234abcd1234",
            "local_alias": "media",
            "key_tier": "admin",  # not allowed; admin is owned by provision_bucket
        },
    )
    assert handler is None


def test_handler_factory_returns_handler_when_complete() -> None:
    handler = make_provision_additional_key_handler(
        _make_config(),
        params={
            "new_key_name": "new-key",
            "bucket_id": "abcd1234abcd1234",
            "local_alias": "media",
            "key_tier": "rw",
        },
    )
    assert handler is not None
    assert callable(handler)
