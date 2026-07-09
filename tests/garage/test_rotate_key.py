"""Tests for stormpulse.garage.jobs.rotate_key.

Covers the four-step orchestrated key rotation (CreateKey, AllowBucketKey,
AddBucketAlias on the new key, DeleteKey on the old):

- happy path: 4 steps, full success payload with new secret
- one test per failure point asserting the correct rollback ran
- permission flags per tier (all / rw / ro)
- rollback-itself-fails cases populating ``manual_cleanup_required``

All Garage interaction is the admin HTTP API (ADR garage/001). As in
``test_state`` / ``test_provision_additional_key``, we patch the ``admin_api``
functions and assert on the recorded calls. ``delete_key`` fires twice in a
full rotation (old key forward, new key in rollback), so the fake uses
one-shot ``fail_next`` rather than fail-all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stormpulse.commands.jobs import JobOutcome
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.jobs.rotate_key import (
    make_rotate_customer_key_handler,
    run_rotate_customer_key,
)

_BUCKET_ID = "f1dc32249aa1d80a"
_OLD_KEY_ID = "GK00000000000000000000old"


def _make_config() -> GarageConfig:
    return GarageConfig(
        enabled=True,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=Path("/opt/garage/garage.toml"),
        admin_url="http://127.0.0.1:3903",
        admin_token="tok",
    )


class _ProgressRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, int, int | None, str]] = []

    async def __call__(
        self, stage: str, current: int, total: int | None, message: str,
        *,
        transfer: object | None = None,
    ) -> None:
        self.events.append((stage, current, total, message))


class _FakeAdmin:
    """Records admin_api calls; ``fail_next(op, err)`` fails the next call to
    ``op`` exactly once. Created keys get a deterministic ``GK`` id."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._fail: dict[str, list[str]] = {}
        self._n = 0

    def fail_next(self, op: str, err: str) -> None:
        self._fail.setdefault(op, []).append(err)

    def _maybe_fail(self, op: str) -> str | None:
        q = self._fail.get(op)
        return q.pop(0) if q else None

    def create_key(
        self, *, admin_url: str, admin_token: str, name: str,
    ) -> tuple[dict[str, Any] | None, str]:
        self.calls.append(("create_key", {"name": name}))
        err = self._maybe_fail("create_key")
        if err is not None:
            return None, err
        self._n += 1
        kid = f"GK{self._n:024d}"
        return {"accessKeyId": kid, "secretAccessKey": "s" * 40, "name": name}, ""

    def allow_bucket_key(
        self, *, admin_url: str, admin_token: str, bucket_ref: str,
        access_key_id: str, read: bool, write: bool, owner: bool = False,
    ) -> tuple[bool, str]:
        self.calls.append(
            ("allow_bucket_key", {
                "bucket_ref": bucket_ref, "access_key_id": access_key_id,
                "read": read, "write": write, "owner": owner,
            })
        )
        err = self._maybe_fail("allow_bucket_key")
        return (False, err) if err is not None else (True, "")

    def deny_bucket_key(
        self, *, admin_url: str, admin_token: str, bucket_ref: str,
        access_key_id: str, read: bool, write: bool, owner: bool = False,
    ) -> tuple[bool, str]:
        self.calls.append(
            ("deny_bucket_key", {
                "bucket_ref": bucket_ref, "access_key_id": access_key_id,
                "read": read, "write": write, "owner": owner,
            })
        )
        err = self._maybe_fail("deny_bucket_key")
        return (False, err) if err is not None else (True, "")

    def add_bucket_alias_local(
        self, *, admin_url: str, admin_token: str, bucket_ref: str,
        access_key_id: str, local_alias: str,
    ) -> tuple[bool, str]:
        self.calls.append(
            ("add_bucket_alias_local", {
                "bucket_ref": bucket_ref, "access_key_id": access_key_id,
                "local_alias": local_alias,
            })
        )
        err = self._maybe_fail("add_bucket_alias_local")
        return (False, err) if err is not None else (True, "")

    def remove_bucket_alias_local(
        self, *, admin_url: str, admin_token: str, bucket_ref: str,
        access_key_id: str, local_alias: str,
    ) -> tuple[bool, str]:
        self.calls.append(
            ("remove_bucket_alias_local", {
                "bucket_ref": bucket_ref, "access_key_id": access_key_id,
                "local_alias": local_alias,
            })
        )
        err = self._maybe_fail("remove_bucket_alias_local")
        return (False, err) if err is not None else (True, "")

    def delete_key(
        self, *, admin_url: str, admin_token: str, access_key_id: str,
    ) -> tuple[bool, str]:
        self.calls.append(("delete_key", {"access_key_id": access_key_id}))
        err = self._maybe_fail("delete_key")
        return (False, err) if err is not None else (True, "")

    def ops(self) -> list[str]:
        return [op for op, _ in self.calls]


def _install(monkeypatch: pytest.MonkeyPatch) -> _FakeAdmin:
    fake = _FakeAdmin()
    for name in (
        "create_key", "allow_bucket_key", "deny_bucket_key",
        "add_bucket_alias_local", "remove_bucket_alias_local", "delete_key",
    ):
        monkeypatch.setattr(
            f"stormpulse.garage.jobs.rotate_key.admin_api.{name}", getattr(fake, name)
        )
    return fake


async def _run(
    fake: _FakeAdmin,
    *,
    key_tier: str = "all",
    config: GarageConfig | None = None,
) -> JobOutcome:
    return await run_rotate_customer_key(
        progress=_ProgressRecorder(),
        garage_config=config or _make_config(),
        old_key_id=_OLD_KEY_ID,
        new_key_name="usr-1-media-rw",
        bucket_id=_BUCKET_ID,
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
    fake = _install(monkeypatch)

    outcome = await _run(fake)

    assert outcome.success is True
    assert outcome.failure_reason is None
    new_key_id = outcome.extras["new_key_id"]
    assert new_key_id is not None and new_key_id.startswith("GK")
    assert outcome.extras["new_secret"]
    assert outcome.extras["new_key_name"] == "usr-1-media-rw"
    assert outcome.extras["step_completed"] == "old_key_delete"
    assert outcome.extras["step_failed"] is None
    assert outcome.extras["rollback_status"] == "not_required"
    assert outcome.extras["manual_cleanup_required"] == []

    assert fake.ops() == [
        "create_key", "allow_bucket_key", "add_bucket_alias_local", "delete_key",
    ]
    # all tier grants read+write+owner
    assert fake.calls[1][1] == {
        "bucket_ref": _BUCKET_ID, "access_key_id": new_key_id,
        "read": True, "write": True, "owner": True,
    }
    assert fake.calls[2][1] == {
        "bucket_ref": _BUCKET_ID, "access_key_id": new_key_id,
        "local_alias": "media-rotated",
    }
    # step 4 deletes the OLD key
    assert fake.calls[3][1] == {"access_key_id": _OLD_KEY_ID}


@pytest.mark.asyncio
async def test_happy_path_tier_rw(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch)
    outcome = await _run(fake, key_tier="rw")
    assert outcome.success is True
    perms = fake.calls[1][1]
    assert (perms["read"], perms["write"], perms["owner"]) == (True, True, False)


@pytest.mark.asyncio
async def test_happy_path_tier_ro(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch)
    outcome = await _run(fake, key_tier="ro")
    assert outcome.success is True
    perms = fake.calls[1][1]
    assert (perms["read"], perms["write"], perms["owner"]) == (True, False, False)


# ---------------------------------------------------------------------------
# Failure points
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step1_new_key_create_failure_no_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch)
    fake.fail_next("create_key", "key create error")

    outcome = await _run(fake)

    assert outcome.success is False
    assert outcome.failure_reason == "new_key_create_failed"
    assert outcome.extras["step_failed"] == "new_key_create"
    assert outcome.extras["new_key_id"] is None
    assert outcome.extras["rollback_status"] == "not_required"
    assert outcome.extras["manual_cleanup_required"] == []
    assert fake.ops() == ["create_key"]


@pytest.mark.asyncio
async def test_step2_permission_grant_failure_deletes_new_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch)
    fake.fail_next("allow_bucket_key", "permission grant error")

    outcome = await _run(fake)
    new_key_id = outcome.extras["new_key_id"]

    assert outcome.failure_reason == "new_key_permission_grant_failed"
    assert outcome.extras["step_failed"] == "new_key_permission_grant"
    assert outcome.extras["rollback_status"] == "complete"
    # create + failed allow + delete new key. No deny (perms not granted),
    # no unalias (alias not attached), no touch of the old key.
    assert fake.ops() == ["create_key", "allow_bucket_key", "delete_key"]
    assert fake.calls[-1][1] == {"access_key_id": new_key_id}


@pytest.mark.asyncio
async def test_step3_alias_attach_failure_revokes_perms_and_deletes_new_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch)
    fake.fail_next("add_bucket_alias_local", "alias attach error")

    outcome = await _run(fake)
    new_key_id = outcome.extras["new_key_id"]

    assert outcome.failure_reason == "new_key_alias_attach_failed"
    assert outcome.extras["step_failed"] == "new_key_alias_attach"
    assert outcome.extras["rollback_status"] == "complete"
    assert fake.ops() == [
        "create_key", "allow_bucket_key", "add_bucket_alias_local",
        "deny_bucket_key", "delete_key",
    ]
    deny = fake.calls[3][1]
    assert (deny["read"], deny["write"], deny["owner"]) == (True, True, True)
    assert fake.calls[-1][1] == {"access_key_id": new_key_id}


@pytest.mark.asyncio
async def test_step4_old_key_delete_failure_full_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 4 (old key delete) is the last forward step. Full rollback."""
    fake = _install(monkeypatch)
    fake.fail_next("delete_key", "old key delete error")  # fails the step-4 old delete

    outcome = await _run(fake)
    new_key_id = outcome.extras["new_key_id"]

    assert outcome.failure_reason == "old_key_delete_failed"
    assert outcome.extras["step_failed"] == "old_key_delete"
    assert outcome.extras["rollback_status"] == "complete"
    assert fake.ops() == [
        "create_key", "allow_bucket_key", "add_bucket_alias_local",
        "delete_key", "remove_bucket_alias_local", "deny_bucket_key", "delete_key",
    ]
    # rollback detaches + denies + deletes the NEW key
    assert fake.calls[4][1] == {
        "bucket_ref": _BUCKET_ID, "access_key_id": new_key_id,
        "local_alias": "media-rotated",
    }
    assert fake.calls[6][1] == {"access_key_id": new_key_id}
    # the only old-key delete was the forward step 4, never in rollback
    assert fake.calls[3][1] == {"access_key_id": _OLD_KEY_ID}


# ---------------------------------------------------------------------------
# Rollback-failure cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_partial_when_unalias_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 4 fails, then rollback's first step (detach alias) also fails."""
    fake = _install(monkeypatch)
    fake.fail_next("delete_key", "old delete error")
    fake.fail_next("remove_bucket_alias_local", "unalias error during rollback")

    outcome = await _run(fake)
    new_key_id = outcome.extras["new_key_id"]

    assert outcome.failure_reason == "rollback_failed"
    assert outcome.extras["step_failed"] == "old_key_delete"
    assert outcome.extras["rollback_status"] == "partial"
    types_ids = _cleanup_pairs(outcome)
    assert ("local_alias", new_key_id) in types_ids
    assert ("permission_grant", new_key_id) in types_ids
    assert ("key", new_key_id) in types_ids


@pytest.mark.asyncio
async def test_rollback_partial_when_perm_revoke_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 4 fails, alias detach succeeds, permission revoke fails."""
    fake = _install(monkeypatch)
    fake.fail_next("delete_key", "old delete error")
    fake.fail_next("deny_bucket_key", "deny error during rollback")

    outcome = await _run(fake)
    new_key_id = outcome.extras["new_key_id"]

    assert outcome.failure_reason == "rollback_failed"
    assert outcome.extras["rollback_status"] == "partial"
    types_ids = _cleanup_pairs(outcome)
    assert ("local_alias", new_key_id) not in types_ids
    assert ("permission_grant", new_key_id) in types_ids
    assert ("key", new_key_id) in types_ids


@pytest.mark.asyncio
async def test_rollback_partial_when_new_key_delete_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 2 fails (perm grant), and rollback's new-key delete also fails."""
    fake = _install(monkeypatch)
    fake.fail_next("allow_bucket_key", "perm grant error")
    fake.fail_next("delete_key", "key delete error during rollback")

    outcome = await _run(fake)
    new_key_id = outcome.extras["new_key_id"]

    assert outcome.failure_reason == "rollback_failed"
    assert outcome.extras["step_failed"] == "new_key_permission_grant"
    assert outcome.extras["rollback_status"] == "partial"
    types_ids = _cleanup_pairs(outcome)
    assert ("key", new_key_id) in types_ids


# ---------------------------------------------------------------------------
# Admin API not configured: fail loud (ADR garage/001)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_api_unconfigured_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch)
    config = _make_config()
    object.__setattr__(config, "admin_url", "")
    object.__setattr__(config, "admin_token", "")

    outcome = await _run(fake, config=config)

    assert outcome.success is False
    assert outcome.failure_reason == "admin_api_unconfigured"
    assert outcome.extras["rollback_status"] == "not_required"
    assert fake.ops() == []


def _cleanup_pairs(outcome: JobOutcome) -> set[tuple[str, str | None]]:
    return {
        (item["type"], item.get("key_id") or item.get("id"))
        for item in outcome.extras["manual_cleanup_required"]
    }


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
