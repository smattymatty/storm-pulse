"""Tests for stormpulse.garage.jobs.detach_account_key.

Detach one account key's grant from a single bucket: deny
read/write/owner, drop the local alias, then read the key back and confirm the
bucket is gone from its grant list. The contract the website relay depends on:

  - deny ok + read-back grant-absent      -> success, confirmed_detached=True
  - deny fails                            -> failure, no read-back
  - deny ok but read-back still granted   -> failure (the deny did not take)
  - alias drop fails but grant confirmed gone -> success, manual_cleanup flagged

As in the other migrated handlers, we patch ``admin_api`` and assert on the
recorded calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stormpulse.commands.jobs import JobOutcome
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.jobs.detach_account_key import (
    make_detach_account_key_handler,
    run_detach_account_key,
)

_BUCKET = "f1dc32249aa1d80a"
_FULL = _BUCKET + "0" * 48
_KEY = "GKaccountkey00000"
_ALIAS = "media"


def _make_config(*, configured: bool = True) -> GarageConfig:
    return GarageConfig(
        enabled=True, container_name="garaged", garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=Path("/opt/garage/garage.toml"),
        admin_url="http://127.0.0.1:3903" if configured else "",
        admin_token="tok" if configured else "",
    )


class _ProgressRecorder:
    async def __call__(self, *a: Any, **k: Any) -> None:
        return None


class _FakeAdmin:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.deny_result: tuple[bool, str] = (True, "")
        self.alias_result: tuple[bool, str] = (True, "")
        # Default read-back: the bucket is gone from the key's grant list.
        self.key_info: tuple[dict[str, Any] | None, str] = ({"buckets": []}, "")

    def deny_bucket_key(self, **kw) -> tuple[bool, str]:
        self.calls.append("deny_bucket_key")
        return self.deny_result

    def remove_bucket_alias_local(self, **kw) -> tuple[bool, str]:
        self.calls.append("remove_bucket_alias_local")
        return self.alias_result

    def get_key_info(self, **kw) -> tuple[dict[str, Any] | None, str]:
        self.calls.append("get_key_info")
        return self.key_info


def _install(monkeypatch: pytest.MonkeyPatch) -> _FakeAdmin:
    fake = _FakeAdmin()
    for name in ("deny_bucket_key", "remove_bucket_alias_local", "get_key_info"):
        monkeypatch.setattr(
            f"stormpulse.garage.jobs.detach_account_key.admin_api.{name}",
            getattr(fake, name),
        )
    return fake


async def _run(fake: _FakeAdmin, *, config: GarageConfig | None = None) -> JobOutcome:
    return await run_detach_account_key(
        progress=_ProgressRecorder(),
        garage_config=config or _make_config(),
        bucket_id=_BUCKET, account_key_id=_KEY, local_alias=_ALIAS,
    )


@pytest.mark.asyncio
async def test_happy_path_grant_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch)
    outcome = await _run(fake)

    assert outcome.success is True
    assert outcome.extras["confirmed_detached"] is True
    assert outcome.extras["alias_removed"] is True
    assert outcome.extras["manual_cleanup_required"] == []
    assert fake.calls == [
        "deny_bucket_key", "remove_bucket_alias_local", "get_key_info",
    ]


@pytest.mark.asyncio
async def test_readback_bucket_present_no_perms_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The bucket may linger in the key's list with every permission false;
    # that is still grant-absent.
    fake = _install(monkeypatch)
    fake.key_info = (
        {"buckets": [{"id": _FULL, "permissions": {
            "read": False, "write": False, "owner": False,
        }}]},
        "",
    )
    outcome = await _run(fake)
    assert outcome.success is True
    assert outcome.extras["confirmed_detached"] is True


@pytest.mark.asyncio
async def test_readback_still_granted_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch)
    fake.key_info = (
        {"buckets": [{"id": _FULL, "permissions": {
            "read": True, "write": True, "owner": True,
        }}]},
        "",
    )
    outcome = await _run(fake)
    assert outcome.success is False
    assert outcome.failure_reason == "grant_still_present"
    assert outcome.extras["confirmed_detached"] is False


@pytest.mark.asyncio
async def test_deny_failure_no_readback(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch)
    fake.deny_result = (False, "HTTP 500: boom")
    outcome = await _run(fake)
    assert outcome.success is False
    assert outcome.failure_reason == "grant_revoke_failed"
    # The security-critical step failed; we never read back or drop the alias.
    assert fake.calls == ["deny_bucket_key"]


@pytest.mark.asyncio
async def test_alias_failure_still_succeeds_with_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The alias drop is cosmetic; its failure does not fail a confirmed detach.
    fake = _install(monkeypatch)
    fake.alias_result = (False, "alias gone already")
    outcome = await _run(fake)
    assert outcome.success is True
    assert outcome.extras["alias_removed"] is False
    assert outcome.extras["manual_cleanup_required"] == [
        {"type": "local_alias", "key_id": _KEY, "alias": _ALIAS},
    ]


@pytest.mark.asyncio
async def test_readback_unreadable_is_unconfirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch)
    fake.key_info = (None, "HTTP 503: unavailable")
    outcome = await _run(fake)
    assert outcome.success is False
    assert outcome.failure_reason == "grant_absence_unconfirmed"


@pytest.mark.asyncio
async def test_unconfigured_admin_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch)
    outcome = await _run(fake, config=_make_config(configured=False))
    assert outcome.success is False
    assert outcome.failure_reason == "admin_api_unconfigured"
    assert fake.calls == []


def test_factory_requires_all_params() -> None:
    cfg = _make_config()
    assert make_detach_account_key_handler(cfg, {}) is None
    assert make_detach_account_key_handler(
        cfg, {"bucket_id": _BUCKET, "account_key_id": _KEY},
    ) is None
    assert make_detach_account_key_handler(cfg, {
        "bucket_id": _BUCKET, "account_key_id": _KEY, "local_alias": _ALIAS,
    }) is not None
