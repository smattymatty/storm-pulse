"""Tests for stormpulse.garage.jobs.provision_additional_key.

Adds a new tiered key (rw or ro) to an existing bucket. Three admin-API steps
with atomic rollback; the bucket itself is never touched by rollback.

  1. CreateKey
  2. AllowBucketKey  (rw -> read+write, ro -> read)
  3. AddBucketAlias  (local variant)

All Garage interaction is the admin HTTP API (ADR garage/001). As in
``test_state``, we patch the ``admin_api`` functions and assert on the recorded
calls and the injected-failure handling, never the CLI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stormpulse.commands.jobs import JobOutcome
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.jobs.provision_additional_key import (
    make_provision_additional_key_handler,
    run_provision_additional_key,
)

_BUCKET_ID = "f1dc32249aa1d80a"  # Storm's 16-char garage_bucket_id


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
        bytes_freed: object | None = None,
    ) -> None:
        self.events.append((stage, current, total, message))


class _FakeAdmin:
    """Records admin_api calls and returns canned successes.

    ``fail[op] = "msg"`` makes that op return its failure shape. Created keys
    get a deterministic ``GK``-prefixed id so tests can assert on identity.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail: dict[str, str] = {}
        self._n = 0

    def create_key(
        self, *, admin_url: str, admin_token: str, name: str,
    ) -> tuple[dict[str, Any] | None, str]:
        self.calls.append(("create_key", {"name": name}))
        if "create_key" in self.fail:
            return None, self.fail["create_key"]
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
        if "allow_bucket_key" in self.fail:
            return False, self.fail["allow_bucket_key"]
        return True, ""

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
        if "deny_bucket_key" in self.fail:
            return False, self.fail["deny_bucket_key"]
        return True, ""

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
        if "add_bucket_alias_local" in self.fail:
            return False, self.fail["add_bucket_alias_local"]
        return True, ""

    def delete_key(
        self, *, admin_url: str, admin_token: str, access_key_id: str,
    ) -> tuple[bool, str]:
        self.calls.append(("delete_key", {"access_key_id": access_key_id}))
        if "delete_key" in self.fail:
            return False, self.fail["delete_key"]
        return True, ""

    def ops(self) -> list[str]:
        return [op for op, _ in self.calls]


def _install(monkeypatch: pytest.MonkeyPatch) -> _FakeAdmin:
    fake = _FakeAdmin()
    for name in (
        "create_key", "allow_bucket_key", "deny_bucket_key",
        "add_bucket_alias_local", "delete_key",
    ):
        monkeypatch.setattr(
            f"stormpulse.garage.jobs.provision_additional_key.admin_api.{name}",
            getattr(fake, name),
        )
    return fake


async def _run(
    fake: _FakeAdmin,
    *,
    new_key_name: str = "new-rw-key",
    bucket_id: str = _BUCKET_ID,
    local_alias: str = "media",
    key_tier: str = "rw",
    config: GarageConfig | None = None,
) -> JobOutcome:
    return await run_provision_additional_key(
        progress=_ProgressRecorder(),
        garage_config=config or _make_config(),
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
    fake = _install(monkeypatch)

    outcome = await _run(fake, key_tier="rw")

    assert outcome.success is True
    assert outcome.failure_reason is None
    new_key_id = outcome.extras["new_key_id"]
    assert new_key_id is not None and new_key_id.startswith("GK")
    assert outcome.extras["new_secret"]
    assert outcome.extras["new_key_name"] == "new-rw-key"
    assert outcome.extras["key_tier"] == "rw"
    assert outcome.extras["step_completed"] == "new_key_alias_attach"
    assert outcome.extras["rollback_status"] == "not_required"

    assert fake.ops() == ["create_key", "allow_bucket_key", "add_bucket_alias_local"]
    assert fake.calls[0][1] == {"name": "new-rw-key"}
    assert fake.calls[1][1] == {
        "bucket_ref": _BUCKET_ID, "access_key_id": new_key_id,
        "read": True, "write": True, "owner": False,
    }
    assert fake.calls[2][1] == {
        "bucket_ref": _BUCKET_ID, "access_key_id": new_key_id, "local_alias": "media",
    }


@pytest.mark.asyncio
async def test_happy_path_all_tier_grants_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Claim-admin: the 'all' tier mints the owner key onto an
    # adopted bucket whose owner slot is free. This is the only tier that
    # grants owner through this handler.
    fake = _install(monkeypatch)

    outcome = await _run(fake, new_key_name="new-admin-key", key_tier="all")

    assert outcome.success is True
    assert outcome.extras["key_tier"] == "all"
    assert fake.calls[1][0] == "allow_bucket_key"
    assert fake.calls[1][1]["read"] is True
    assert fake.calls[1][1]["write"] is True
    assert fake.calls[1][1]["owner"] is True


@pytest.mark.asyncio
async def test_happy_path_ro_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch)

    outcome = await _run(fake, new_key_name="new-ro-key", key_tier="ro")

    assert outcome.success is True
    assert outcome.extras["key_tier"] == "ro"
    # ro grants read only.
    assert fake.calls[1][0] == "allow_bucket_key"
    assert fake.calls[1][1]["read"] is True
    assert fake.calls[1][1]["write"] is False
    assert fake.calls[1][1]["owner"] is False


# ---------------------------------------------------------------------------
# Failure points
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step1_key_create_failure_no_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch)
    fake.fail["create_key"] = "key create error"

    outcome = await _run(fake)

    assert outcome.success is False
    assert outcome.failure_reason == "new_key_create_failed"
    assert outcome.extras["step_failed"] == "new_key_create"
    assert outcome.extras["new_key_id"] is None
    assert outcome.extras["rollback_status"] == "not_required"
    assert fake.ops() == ["create_key"]


@pytest.mark.asyncio
async def test_step2_perm_grant_failure_deletes_new_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch)
    fake.fail["allow_bucket_key"] = "perm grant error"

    outcome = await _run(fake)
    new_key_id = outcome.extras["new_key_id"]

    assert outcome.failure_reason == "new_key_permission_grant_failed"
    assert outcome.extras["step_failed"] == "new_key_permission_grant"
    assert outcome.extras["rollback_status"] == "complete"
    # create + failed allow + delete; no deny (no perms granted).
    assert fake.ops() == ["create_key", "allow_bucket_key", "delete_key"]
    assert fake.calls[-1][1] == {"access_key_id": new_key_id}


@pytest.mark.asyncio
async def test_step3_alias_attach_failure_revokes_and_deletes_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch)
    fake.fail["add_bucket_alias_local"] = "alias attach error"

    outcome = await _run(fake)
    new_key_id = outcome.extras["new_key_id"]

    assert outcome.failure_reason == "new_key_alias_attach_failed"
    assert outcome.extras["step_failed"] == "new_key_alias_attach"
    assert outcome.extras["rollback_status"] == "complete"
    assert fake.ops() == [
        "create_key", "allow_bucket_key", "add_bucket_alias_local",
        "deny_bucket_key", "delete_key",
    ]
    # rw tier denies read+write.
    deny = fake.calls[3][1]
    assert deny["read"] is True and deny["write"] is True
    assert fake.calls[-1][1] == {"access_key_id": new_key_id}


# ---------------------------------------------------------------------------
# Rollback-failure cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_partial_when_perm_revoke_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 3 fails; rollback's deny also fails."""
    fake = _install(monkeypatch)
    fake.fail["add_bucket_alias_local"] = "alias attach error"
    fake.fail["deny_bucket_key"] = "deny error during rollback"

    outcome = await _run(fake)
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
    # The bucket is never this orchestrator's to clean up.
    assert not any(t == "bucket" for t, _ in types_ids)


@pytest.mark.asyncio
async def test_rollback_partial_when_key_delete_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 2 fails; rollback's key delete also fails."""
    fake = _install(monkeypatch)
    fake.fail["allow_bucket_key"] = "perm grant error"
    fake.fail["delete_key"] = "key delete error during rollback"

    outcome = await _run(fake)
    new_key_id = outcome.extras["new_key_id"]

    assert outcome.failure_reason == "rollback_failed"
    assert outcome.extras["step_failed"] == "new_key_permission_grant"
    assert outcome.extras["rollback_status"] == "partial"
    cleanup = outcome.extras["manual_cleanup_required"]
    types_ids = {
        (item["type"], item.get("key_id") or item.get("id")) for item in cleanup
    }
    assert ("key", new_key_id) in types_ids
    # Perms were never granted, so no permission_grant entry.
    assert not any(t == "permission_grant" for t, _ in types_ids)


# ---------------------------------------------------------------------------
# Admin API not configured: fail loud, never a silent no-op (ADR garage/001).
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
    # No Garage calls were attempted.
    assert fake.ops() == []


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
