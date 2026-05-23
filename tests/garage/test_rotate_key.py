"""Tests for stormpulse.garage.rotate_key.

Covers the four-step orchestrated key rotation:

- happy path: 4 steps, full success payload with new secret
- one test per failure point asserting the correct rollback ran
- tests for permission flags per tier (all / rw / ro)
- one test where rollback itself fails partway and ``manual_cleanup_required``
  is correctly populated

Backed by ``FakeGarage`` (see ``tests/garage/_fake_garage.py``). Tests
that surface real orchestrator bugs are marked
``pytest.mark.skip`` with reasons pointing at the follow-up PR. The
fake is **not** weakened to make them pass.

Important: ``rotate_key.py:43`` does
``from stormpulse.garage.provision_bucket import run_garage``, which
creates a local binding ``rotate_key.run_garage`` distinct from
``provision_bucket.run_garage``. This file patches ``rotate_key``
directly. ``test_provision_bucket.py`` patches ``provision_bucket``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stormpulse.commands.jobs import JobOutcome
from stormpulse.config import GarageConfig
from stormpulse.garage import rotate_key
from stormpulse.garage.rotate_key import (
    make_rotate_customer_key_handler,
    run_rotate_customer_key,
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


def _setup_fake() -> tuple[FakeGarage, str, str]:
    """Create a fake with the bucket and old key already provisioned.

    Returns (fake, bucket_alias, old_key_id) - both are the references
    to pass into run_rotate_customer_key. The bucket alias is the
    real-world reference shape (we rely on rule 8: alias is a valid
    bucket reference).
    """
    fake = FakeGarage()
    fake.add_bucket("media")
    old_key = fake.add_key("usr-1-media-rw-old")
    return fake, "media", old_key.key_id


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeGarage,
    bucket_ref: str,
    old_key_id: str,
    *,
    key_tier: str = "all",
) -> JobOutcome:
    monkeypatch.setattr(rotate_key, "run_garage", fake.run_garage)
    return await run_rotate_customer_key(
        progress=_ProgressRecorder(),
        garage_config=_make_config(),
        old_key_id=old_key_id,
        new_key_name="usr-1-media-rw",
        bucket_id=bucket_ref,
        local_alias="media-rotated",
        key_tier=key_tier,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_new_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, bucket_ref, old_key_id = _setup_fake()

    outcome = await _run(monkeypatch, fake, bucket_ref, old_key_id)

    assert outcome.success is True
    assert outcome.failure_reason is None
    new_key_id = outcome.extras["new_key_id"]
    assert new_key_id is not None
    assert new_key_id.startswith("GK")
    assert outcome.extras["new_secret"]
    assert outcome.extras["new_key_name"] == "usr-1-media-rw"
    assert outcome.extras["step_completed"] == "old_key_delete"
    assert outcome.extras["step_failed"] is None
    assert outcome.extras["rollback_status"] == "not_required"
    assert outcome.extras["manual_cleanup_required"] == []

    assert len(fake.calls) == 4
    assert fake.calls[0] == ("key", "create", "usr-1-media-rw")
    assert fake.calls[1] == (
        "bucket", "allow", "--read", "--write", "--owner",
        bucket_ref, "--key", new_key_id,
    )
    assert fake.calls[2] == (
        "bucket", "alias", "--local", new_key_id, bucket_ref, "media-rotated",
    )
    assert fake.calls[3] == ("key", "delete", "--yes", old_key_id)
    # Old key actually got removed from fake state
    assert old_key_id not in fake.keys
    # New key now has the granted perms
    bucket = next(iter(fake.buckets.values()))
    assert fake.keys[new_key_id].permissions[bucket.bucket_id] == {
        "read", "write", "owner",
    }


@pytest.mark.asyncio
async def test_happy_path_tier_rw_uses_read_write_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, bucket_ref, old_key_id = _setup_fake()

    outcome = await _run(
        monkeypatch, fake, bucket_ref, old_key_id, key_tier="rw",
    )

    assert outcome.success is True
    new_key_id = outcome.extras["new_key_id"]
    assert fake.calls[1] == (
        "bucket", "allow", "--read", "--write",
        bucket_ref, "--key", new_key_id,
    )
    assert "--owner" not in fake.calls[1]


@pytest.mark.asyncio
async def test_happy_path_tier_ro_uses_read_only_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, bucket_ref, old_key_id = _setup_fake()

    outcome = await _run(
        monkeypatch, fake, bucket_ref, old_key_id, key_tier="ro",
    )

    assert outcome.success is True
    new_key_id = outcome.extras["new_key_id"]
    assert fake.calls[1] == (
        "bucket", "allow", "--read",
        bucket_ref, "--key", new_key_id,
    )
    assert "--write" not in fake.calls[1]
    assert "--owner" not in fake.calls[1]


# ---------------------------------------------------------------------------
# Failure points
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step1_new_key_create_failure_no_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, bucket_ref, old_key_id = _setup_fake()
    fake.fail_next("key_create", stderr="key create error")

    outcome = await _run(monkeypatch, fake, bucket_ref, old_key_id)

    assert outcome.success is False
    assert outcome.failure_reason == "new_key_create_failed"
    assert outcome.extras["step_failed"] == "new_key_create"
    assert outcome.extras["new_key_id"] is None
    assert outcome.extras["rollback_status"] == "not_required"
    assert outcome.extras["manual_cleanup_required"] == []
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_step2_permission_grant_failure_deletes_new_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, bucket_ref, old_key_id = _setup_fake()
    fake.fail_next("bucket_allow", stderr="permission grant error")

    outcome = await _run(monkeypatch, fake, bucket_ref, old_key_id)
    new_key_id = outcome.extras["new_key_id"]

    assert outcome.failure_reason == "new_key_permission_grant_failed"
    assert outcome.extras["step_failed"] == "new_key_permission_grant"
    assert new_key_id is not None
    assert outcome.extras["rollback_status"] == "complete"
    assert len(fake.calls) == 3
    rollback_calls = fake.calls[2:]
    assert rollback_calls == [("key", "delete", "--yes", new_key_id)]
    # Old key never touched
    delete_calls = [c for c in fake.calls if c[:2] == ("key", "delete")]
    assert ("key", "delete", "--yes", old_key_id) not in delete_calls
    # New key was actually deleted from fake state
    assert new_key_id not in fake.keys
    # Old key still alive
    assert old_key_id in fake.keys


@pytest.mark.asyncio
async def test_step3_alias_attach_failure_revokes_perms_and_deletes_new_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake, bucket_ref, old_key_id = _setup_fake()
    fake.fail_next("bucket_alias_local", stderr="alias attach error")

    outcome = await _run(monkeypatch, fake, bucket_ref, old_key_id)
    new_key_id = outcome.extras["new_key_id"]

    assert outcome.failure_reason == "new_key_alias_attach_failed"
    assert outcome.extras["step_failed"] == "new_key_alias_attach"
    assert new_key_id is not None
    assert outcome.extras["rollback_status"] == "complete"
    rollback_calls = fake.calls[3:]
    assert rollback_calls[0] == (
        "bucket", "deny", "--read", "--write", "--owner",
        bucket_ref, "--key", new_key_id,
    )
    assert rollback_calls[1] == ("key", "delete", "--yes", new_key_id)
    delete_calls = [c for c in fake.calls if c[:2] == ("key", "delete")]
    assert ("key", "delete", "--yes", old_key_id) not in delete_calls
    # New key gone, old key alive
    assert new_key_id not in fake.keys
    assert old_key_id in fake.keys


@pytest.mark.asyncio
async def test_step4_old_key_delete_failure_full_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 4 (old key delete) is the last forward step. Full rollback."""
    fake, bucket_ref, old_key_id = _setup_fake()
    fake.fail_next("key_delete", stderr="old key delete error")

    outcome = await _run(monkeypatch, fake, bucket_ref, old_key_id)
    new_key_id = outcome.extras["new_key_id"]

    assert outcome.failure_reason == "old_key_delete_failed"
    assert outcome.extras["step_failed"] == "old_key_delete"
    assert outcome.extras["rollback_status"] == "complete"
    rollback_calls = fake.calls[4:]
    assert len(rollback_calls) == 3
    assert rollback_calls[0] == (
        "bucket", "unalias", "--local", new_key_id, "media-rotated",
    )
    assert rollback_calls[1] == (
        "bucket", "deny", "--read", "--write", "--owner",
        bucket_ref, "--key", new_key_id,
    )
    assert rollback_calls[2] == ("key", "delete", "--yes", new_key_id)
    for call in rollback_calls:
        assert old_key_id not in call


# ---------------------------------------------------------------------------
# Rollback-failure cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_partial_when_unalias_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 4 fails, then rollback's first step (unalias) is also injected
    to fail. ``fail_next`` short-circuits the fake's dispatcher, so the
    underlying Change D 3-positional bug is masked here - this test
    verifies the orchestrator's rollback-failure handling, not the
    unalias signature.
    """
    fake, bucket_ref, old_key_id = _setup_fake()
    fake.fail_next("key_delete", stderr="old delete error")
    fake.fail_next("bucket_unalias_local", stderr="unalias error during rollback")

    outcome = await _run(monkeypatch, fake, bucket_ref, old_key_id)
    new_key_id = outcome.extras["new_key_id"]

    assert outcome.failure_reason == "rollback_failed"
    assert outcome.extras["step_failed"] == "old_key_delete"
    assert outcome.extras["rollback_status"] == "partial"
    cleanup = outcome.extras["manual_cleanup_required"]
    types_ids = {(item["type"], item.get("key_id") or item.get("id"))
                 for item in cleanup}
    assert ("local_alias", new_key_id) in types_ids
    assert ("permission_grant", new_key_id) in types_ids
    assert ("key", new_key_id) in types_ids


@pytest.mark.asyncio
async def test_rollback_partial_when_perm_revoke_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 4 fails, alias detach succeeds, permission revoke fails."""
    fake, bucket_ref, old_key_id = _setup_fake()
    fake.fail_next("key_delete", stderr="old delete error")
    fake.fail_next("bucket_deny", stderr="deny error during rollback")

    outcome = await _run(monkeypatch, fake, bucket_ref, old_key_id)
    new_key_id = outcome.extras["new_key_id"]

    assert outcome.failure_reason == "rollback_failed"
    assert outcome.extras["rollback_status"] == "partial"
    cleanup = outcome.extras["manual_cleanup_required"]
    types_ids = {(item["type"], item.get("key_id") or item.get("id"))
                 for item in cleanup}
    assert ("local_alias", new_key_id) not in types_ids
    assert ("permission_grant", new_key_id) in types_ids
    assert ("key", new_key_id) in types_ids


@pytest.mark.asyncio
async def test_rollback_partial_when_new_key_delete_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 2 fails (perm grant), and rollback's new-key delete also fails.

    No unalias path involved (step 3 didn't run), so Change D doesn't
    surface here.
    """
    fake, bucket_ref, old_key_id = _setup_fake()
    fake.fail_next("bucket_allow", stderr="perm grant error")
    fake.fail_next("key_delete", stderr="key delete error during rollback")

    outcome = await _run(monkeypatch, fake, bucket_ref, old_key_id)
    new_key_id = outcome.extras["new_key_id"]

    assert outcome.failure_reason == "rollback_failed"
    assert outcome.extras["step_failed"] == "new_key_permission_grant"
    assert outcome.extras["rollback_status"] == "partial"
    cleanup = outcome.extras["manual_cleanup_required"]
    types_ids = {(item["type"], item.get("key_id") or item.get("id"))
                 for item in cleanup}
    assert ("key", new_key_id) in types_ids


# ---------------------------------------------------------------------------
# Handler factory
# ---------------------------------------------------------------------------


def test_handler_factory_returns_none_on_missing_params() -> None:
    handler = make_rotate_customer_key_handler(
        _make_config(),
        params={"old_key_id": "GK_OLD"},
    )
    assert handler is None


def test_handler_factory_returns_none_when_key_tier_missing() -> None:
    handler = make_rotate_customer_key_handler(
        _make_config(),
        params={
            "old_key_id": "GK_OLD",
            "new_key_name": "new-key",
            "bucket_id": "media",
            "local_alias": "media",
        },
    )
    assert handler is None


def test_handler_factory_returns_none_on_invalid_key_tier() -> None:
    handler = make_rotate_customer_key_handler(
        _make_config(),
        params={
            "old_key_id": "GK_OLD",
            "new_key_name": "new-key",
            "bucket_id": "media",
            "local_alias": "media",
            "key_tier": "admin",  # not one of all/rw/ro
        },
    )
    assert handler is None


def test_handler_factory_returns_handler_when_complete() -> None:
    handler = make_rotate_customer_key_handler(
        _make_config(),
        params={
            "old_key_id": "GK_OLD",
            "new_key_name": "new-key",
            "bucket_id": "media",
            "local_alias": "media",
            "key_tier": "all",
        },
    )
    assert handler is not None
    assert callable(handler)
