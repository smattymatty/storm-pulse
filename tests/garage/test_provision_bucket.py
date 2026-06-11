"""Tests for stormpulse.garage.provision_bucket.

Provisions a bucket and its admin key atomically via the admin HTTP API (ADR
garage/001), two calls:

  1. CreateKey(admin)
  2. CreateBucket(localAlias={accessKeyId, alias: display, allow: rwo})

The single CreateBucket binds the admin key's local alias and grants its
permissions in one transaction, so there is no separate allow/alias/unalias and
no throwaway alias. Rollback is one step: a failed bucket create deletes the
orphan key. Additional keys (rw/ro) come later via provision_additional_key.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stormpulse.commands.jobs import JobOutcome
from stormpulse.config import GarageConfig
from stormpulse.garage.provision_bucket import (
    make_provision_customer_bucket_handler,
    run_provision_customer_bucket,
)


def _make_config() -> GarageConfig:
    return GarageConfig(
        enabled=True,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=Path("/opt/garage/garage.toml"),
        state_push_interval_seconds=300,
        admin_url="http://127.0.0.1:3903",
        admin_token="tok",
    )


class _ProgressRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, int, int | None, str]] = []

    async def __call__(
        self, stage: str, current: int, total: int | None, message: str,
    ) -> None:
        self.events.append((stage, current, total, message))


class _FakeAdmin:
    """Records admin_api calls; ``fail[op] = err`` fails that op. Each op is
    called at most once per run, so fail-all is sufficient."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail: dict[str, str] = {}
        self._n = 0
        self._b = 0

    def create_key(
        self, *, admin_url: str, admin_token: str, name: str,
    ) -> tuple[dict[str, Any] | None, str]:
        self.calls.append(("create_key", {"name": name}))
        if "create_key" in self.fail:
            return None, self.fail["create_key"]
        self._n += 1
        return {"accessKeyId": f"GK{self._n:024d}", "secretAccessKey": "s" * 40}, ""

    def create_bucket(
        self,
        *,
        admin_url: str,
        admin_token: str,
        local_alias: dict[str, Any] | None = None,
        global_alias: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        self.calls.append(
            ("create_bucket", {"local_alias": local_alias, "global_alias": global_alias})
        )
        if "create_bucket" in self.fail:
            return None, self.fail["create_bucket"]
        self._b += 1
        return {"id": f"{self._b:064x}", "globalAliases": []}, ""

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
    for name in ("create_key", "create_bucket", "delete_key"):
        monkeypatch.setattr(
            f"stormpulse.garage.provision_bucket.admin_api.{name}",
            getattr(fake, name),
        )
    return fake


async def _run(
    fake: _FakeAdmin, *, config: GarageConfig | None = None,
) -> JobOutcome:
    return await run_provision_customer_bucket(
        progress=_ProgressRecorder(),
        garage_config=config or _make_config(),
        display_name="media",
        key_name_admin="key-admin",
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_full_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch)
    outcome = await _run(fake)

    assert outcome.success is True
    assert outcome.exit_code == 0
    assert outcome.failure_reason is None
    bucket_uuid = outcome.extras["bucket_uuid"]
    assert bucket_uuid is not None and len(bucket_uuid) == 16
    assert outcome.extras["admin"]["key_name"] == "key-admin"
    assert outcome.extras["admin"]["key_id"].startswith("GK")
    assert outcome.extras["admin"]["secret"]
    assert outcome.extras["step_completed"] == "bucket_create"
    assert outcome.extras["step_failed"] is None
    assert outcome.extras["rollback_status"] == "not_required"
    assert outcome.extras["manual_cleanup_required"] == []
    assert "rw" not in outcome.extras
    assert "ro" not in outcome.extras


@pytest.mark.asyncio
async def test_happy_path_atomic_create_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch)
    outcome = await _run(fake)
    admin_key = outcome.extras["admin"]["key_id"]

    # Exactly two calls: key first (the alias needs its id), then the atomic
    # bucket create that binds the alias and grants perms.
    assert fake.ops() == ["create_key", "create_bucket"]
    assert fake.calls[0][1] == {"name": "key-admin"}
    assert fake.calls[1][1] == {
        "local_alias": {
            "accessKeyId": admin_key,
            "alias": "media",
            "allow": {"read": True, "write": True, "owner": True},
        },
        "global_alias": None,
    }


# ---------------------------------------------------------------------------
# Failure points
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_key_create_failure_no_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch)
    fake.fail["create_key"] = "key create error"

    outcome = await _run(fake)

    assert outcome.success is False
    assert outcome.failure_reason == "admin_key_create_failed"
    assert outcome.extras["step_failed"] == "admin_key_create"
    assert outcome.extras["step_completed"] is None
    assert outcome.extras["bucket_uuid"] is None
    assert outcome.extras["rollback_status"] == "not_required"
    assert fake.ops() == ["create_key"]


@pytest.mark.asyncio
async def test_bucket_create_failure_deletes_orphan_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch)
    fake.fail["create_bucket"] = "bucket create error"

    outcome = await _run(fake)

    assert outcome.failure_reason == "bucket_create_failed"
    assert outcome.extras["step_failed"] == "bucket_create"
    assert outcome.extras["step_completed"] == "admin_key_create"
    assert outcome.extras["rollback_status"] == "complete"
    assert outcome.extras["bucket_uuid"] is None
    assert outcome.extras.get("admin") is None  # not exposed on failure
    assert fake.ops() == ["create_key", "create_bucket", "delete_key"]
    # The deleted key is the orphan admin key just created.
    assert fake.calls[-1][1]["access_key_id"].startswith("GK")


@pytest.mark.asyncio
async def test_bucket_create_failure_rollback_partial_when_key_delete_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch)
    fake.fail["create_bucket"] = "bucket create error"
    fake.fail["delete_key"] = "key delete error during rollback"

    outcome = await _run(fake)

    assert outcome.failure_reason == "rollback_failed"
    assert outcome.extras["step_failed"] == "bucket_create"
    assert outcome.extras["rollback_status"] == "partial"
    cleanup = outcome.extras["manual_cleanup_required"]
    types = {item["type"] for item in cleanup}
    assert types == {"key"}
    assert fake.ops() == ["create_key", "create_bucket", "delete_key"]


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
        params={"display_name": "media", "key_name_admin": "k-a"},
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
            "key_name_rw": "k-rw",
            "key_name_ro": "k-ro",
        },
    )
    assert handler is not None
