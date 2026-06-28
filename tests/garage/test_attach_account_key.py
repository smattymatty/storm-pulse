"""Tests for stormpulse.garage.attach_account_key.

Attach grants an account key a chosen tier on an existing bucket (BUCKETS-014),
the inverse of detach: AllowBucketKey + alias-add + grant-present read-back.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stormpulse.commands.jobs import JobOutcome
from stormpulse.garage.attach_account_key import (
    make_attach_account_key_handler,
    run_attach_account_key,
)
from stormpulse.garage.config import GarageConfig

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


class _Progress:
    async def __call__(self, *a) -> None:
        return None


class _FakeAdmin:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.allow_result: tuple[bool, str] = (True, "")
        self.deny_result: tuple[bool, str] = (True, "")
        self.alias_result: tuple[bool, str] = (True, "")
        # Default read-back: the key now holds rw on the bucket.
        self.key_info: tuple[dict[str, Any] | None, str] = (
            {"buckets": [{"id": _FULL, "permissions": {
                "read": True, "write": True, "owner": False,
            }}]}, "",
        )

    def allow_bucket_key(self, *, read, write, owner, **kw):
        self.calls.append(("allow_bucket_key", {"read": read, "write": write, "owner": owner}))
        return self.allow_result

    def deny_bucket_key(self, *, read, write, owner, **kw):
        self.calls.append(("deny_bucket_key", {"read": read, "write": write, "owner": owner}))
        return self.deny_result

    def add_bucket_alias_local(self, **kw):
        self.calls.append(("add_bucket_alias_local", {}))
        return self.alias_result

    def get_key_info(self, **kw):
        self.calls.append(("get_key_info", {}))
        return self.key_info

    def ops(self):
        return [op for op, _ in self.calls]


def _install(monkeypatch):
    fake = _FakeAdmin()
    for name in ("allow_bucket_key", "deny_bucket_key", "add_bucket_alias_local", "get_key_info"):
        monkeypatch.setattr(
            f"stormpulse.garage.attach_account_key.admin_api.{name}",
            getattr(fake, name),
        )
    return fake


async def _run(fake, *, tier="rw", config=None) -> JobOutcome:
    return await run_attach_account_key(
        progress=_Progress(), garage_config=config or _make_config(),
        bucket_id=_BUCKET, account_key_id=_KEY, local_alias=_ALIAS, tier=tier,
    )


@pytest.mark.asyncio
async def test_attach_rw_confirmed(monkeypatch):
    fake = _install(monkeypatch)
    outcome = await _run(fake, tier="rw")
    assert outcome.success is True
    assert outcome.extras["confirmed_attached"] is True
    assert outcome.extras["tier"] == "rw"
    grant = next(c for c in fake.calls if c[0] == "allow_bucket_key")
    assert grant[1] == {"read": True, "write": True, "owner": False}
    # Precise set: rw denies the complement (owner) so a re-attach can NARROW.
    narrow = next(c for c in fake.calls if c[0] == "deny_bucket_key")
    assert narrow[1] == {"read": False, "write": False, "owner": True}
    assert fake.ops() == ["allow_bucket_key", "deny_bucket_key", "add_bucket_alias_local", "get_key_info"]


@pytest.mark.asyncio
async def test_attach_ro_grants_read_only(monkeypatch):
    fake = _install(monkeypatch)
    fake.key_info = ({"buckets": [{"id": _FULL, "permissions": {
        "read": True, "write": False, "owner": False,
    }}]}, "")
    outcome = await _run(fake, tier="ro")
    assert outcome.success is True
    grant = next(c for c in fake.calls if c[0] == "allow_bucket_key")
    assert grant[1] == {"read": True, "write": False, "owner": False}
    # ro denies write + owner: the narrow that makes a change-scope to ro real.
    narrow = next(c for c in fake.calls if c[0] == "deny_bucket_key")
    assert narrow[1] == {"read": False, "write": True, "owner": True}


@pytest.mark.asyncio
async def test_attach_owner(monkeypatch):
    fake = _install(monkeypatch)
    fake.key_info = ({"buckets": [{"id": _FULL, "permissions": {
        "read": True, "write": True, "owner": True,
    }}]}, "")
    outcome = await _run(fake, tier="owner")
    assert outcome.success is True
    grant = next(c for c in fake.calls if c[0] == "allow_bucket_key")
    assert grant[1]["owner"] is True
    # owner is the full set: nothing to deny, no narrow call.
    assert "deny_bucket_key" not in fake.ops()


@pytest.mark.asyncio
async def test_grant_failure_no_readback(monkeypatch):
    fake = _install(monkeypatch)
    fake.allow_result = (False, "HTTP 500: boom")
    outcome = await _run(fake)
    assert outcome.success is False
    assert outcome.failure_reason == "grant_failed"
    assert fake.calls and fake.calls[0][0] == "allow_bucket_key"
    assert "get_key_info" not in fake.ops()


@pytest.mark.asyncio
async def test_readback_missing_grant_fails(monkeypatch):
    fake = _install(monkeypatch)
    fake.key_info = ({"buckets": []}, "")  # grant didn't land
    outcome = await _run(fake)
    assert outcome.success is False
    assert outcome.failure_reason == "grant_not_present"


@pytest.mark.asyncio
async def test_alias_failure_still_succeeds_with_cleanup(monkeypatch):
    fake = _install(monkeypatch)
    fake.alias_result = (False, "alias exists")
    outcome = await _run(fake)
    assert outcome.success is True
    assert outcome.extras["alias_added"] is False
    assert outcome.extras["manual_cleanup_required"][0]["type"] == "local_alias"


@pytest.mark.asyncio
async def test_readback_unreadable_unconfirmed(monkeypatch):
    fake = _install(monkeypatch)
    fake.key_info = (None, "HTTP 503: unavailable")
    outcome = await _run(fake)
    assert outcome.success is False
    assert outcome.failure_reason == "grant_unconfirmed"


@pytest.mark.asyncio
async def test_unconfigured_fails(monkeypatch):
    fake = _install(monkeypatch)
    outcome = await _run(fake, config=_make_config(configured=False))
    assert outcome.success is False
    assert outcome.failure_reason == "admin_api_unconfigured"
    assert fake.calls == []


def test_factory_requires_params_and_valid_tier():
    cfg = _make_config()
    assert make_attach_account_key_handler(cfg, {}) is None
    assert make_attach_account_key_handler(cfg, {
        "bucket_id": _BUCKET, "account_key_id": _KEY, "local_alias": _ALIAS,
        "tier": "admin",  # invalid
    }) is None
    assert make_attach_account_key_handler(cfg, {
        "bucket_id": _BUCKET, "account_key_id": _KEY, "local_alias": _ALIAS,
        "tier": "rw",
    }) is not None
