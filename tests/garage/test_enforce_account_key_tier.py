"""Tests for stormpulse.garage.enforce_account_key_tier (BUCKETS-016 Slice 4).

Narrows an account key's over-tier grants down to its tier. All-or-nothing on
stranding, idempotent when already enforced. We patch the admin_api reads/writes
and assert on the recorded calls, never the CLI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stormpulse.commands.jobs import JobOutcome
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.enforce_account_key_tier import (
    make_enforce_account_key_tier_handler,
    run_enforce_account_key_tier,
)

_KEY = "GKaccountkey00000"
_OTHER = "GKotherowner00000"
_FULL1 = "a" * 64


def _make_config(*, configured: bool = True) -> GarageConfig:
    return GarageConfig(
        enabled=True, container_name="garaged", garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=Path("/opt/garage/garage.toml"),
        admin_url="http://127.0.0.1:3903" if configured else "",
        admin_token="tok" if configured else "",
    )


class _Progress:
    async def __call__(self, *a: Any) -> None:
        return None


class _FakeAdmin:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # The key's own grants. Default: owner on _FULL1.
        self.key_buckets: list[dict[str, Any]] = [
            {"id": _FULL1, "permissions": {"read": True, "write": True, "owner": True}},
        ]
        self.key_info_err = ""
        # Per-bucket key list. Default: _FULL1 has a SECOND owner (safe to narrow).
        self.bucket_keys: dict[str, list[dict[str, Any]]] = {
            _FULL1: [
                {"accessKeyId": _KEY, "permissions": {"read": True, "write": True, "owner": True}},
                {"accessKeyId": _OTHER, "permissions": {"read": True, "write": True, "owner": True}},
            ],
        }
        self.allow_result: tuple[bool, str] = (True, "")
        self.deny_result: tuple[bool, str] = (True, "")

    def get_key_info(self, *, access_key_id: str, **kw: Any) -> tuple[dict[str, Any] | None, str]:
        self.calls.append(("get_key_info", {"access_key_id": access_key_id}))
        if self.key_info_err:
            return None, self.key_info_err
        return {"buckets": self.key_buckets}, ""

    def get_bucket_info(self, *, bucket_ref: str, **kw: Any) -> tuple[dict[str, Any] | None, str]:
        self.calls.append(("get_bucket_info", {"bucket_ref": bucket_ref}))
        return {"keys": self.bucket_keys.get(bucket_ref, [])}, ""

    def allow_bucket_key(self, *, bucket_ref: str, read: bool, write: bool, owner: bool, **kw: Any) -> tuple[bool, str]:
        self.calls.append(("allow_bucket_key", {"bucket_ref": bucket_ref, "read": read, "write": write, "owner": owner}))
        return self.allow_result

    def deny_bucket_key(self, *, bucket_ref: str, read: bool, write: bool, owner: bool, **kw: Any) -> tuple[bool, str]:
        self.calls.append(("deny_bucket_key", {"bucket_ref": bucket_ref, "read": read, "write": write, "owner": owner}))
        return self.deny_result

    def ops(self) -> list[str]:
        return [op for op, _ in self.calls]


def _install(monkeypatch: pytest.MonkeyPatch) -> _FakeAdmin:
    fake = _FakeAdmin()
    for name in ("get_key_info", "get_bucket_info", "allow_bucket_key", "deny_bucket_key"):
        monkeypatch.setattr(
            f"stormpulse.garage.enforce_account_key_tier.admin_api.{name}",
            getattr(fake, name),
        )
    return fake


async def _run(fake: _FakeAdmin, *, tier: str = "rw", config: GarageConfig | None = None) -> JobOutcome:
    return await run_enforce_account_key_tier(
        progress=_Progress(), garage_config=config or _make_config(),
        account_key_id=_KEY, tier=tier,
    )


@pytest.mark.asyncio
async def test_idempotent_noop_when_already_at_tier(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch)
    # Key holds only ro on _FULL1; tier rw, so nothing exceeds.
    fake.key_buckets = [{"id": _FULL1, "permissions": {"read": True, "write": False, "owner": False}}]
    outcome = await _run(fake, tier="rw")
    assert outcome.success is True
    assert outcome.extras["narrowed_buckets"] == []
    assert "allow_bucket_key" not in fake.ops()
    assert "deny_bucket_key" not in fake.ops()


@pytest.mark.asyncio
async def test_narrows_owner_to_rw_when_another_owner_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch)
    outcome = await _run(fake, tier="rw")
    assert outcome.success is True
    assert outcome.extras["narrowed_buckets"] == [_FULL1[:16]]
    allow = next(c for c in fake.calls if c[0] == "allow_bucket_key")
    assert allow[1] == {"bucket_ref": _FULL1, "read": True, "write": True, "owner": False}
    # Deny the complement (owner) so it lands EXACTLY at rw.
    deny = next(c for c in fake.calls if c[0] == "deny_bucket_key")
    assert deny[1] == {"bucket_ref": _FULL1, "read": False, "write": False, "owner": True}


@pytest.mark.asyncio
async def test_aborts_all_or_nothing_when_would_strand(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch)
    # _FULL1 owner, and this key is the ONLY owner: narrowing would strand it.
    fake.bucket_keys = {
        _FULL1: [{"accessKeyId": _KEY, "permissions": {"read": True, "write": True, "owner": True}}],
    }
    outcome = await _run(fake, tier="ro")
    assert outcome.success is False
    assert outcome.failure_reason == "would_strand"
    assert _FULL1[:16] in outcome.extras["strand_risk"]
    # All-or-nothing: NOT a single grant touched.
    assert "allow_bucket_key" not in fake.ops()
    assert "deny_bucket_key" not in fake.ops()


@pytest.mark.asyncio
async def test_owner_tier_narrows_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch)
    outcome = await _run(fake, tier="all")
    assert outcome.success is True
    assert outcome.extras["narrowed_buckets"] == []
    assert "allow_bucket_key" not in fake.ops()


@pytest.mark.asyncio
async def test_read_failure_makes_no_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch)
    fake.key_info_err = "HTTP 503: unavailable"
    outcome = await _run(fake)
    assert outcome.success is False
    assert outcome.failure_reason == "read_failed"
    assert "allow_bucket_key" not in fake.ops()


@pytest.mark.asyncio
async def test_unconfigured_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch)
    outcome = await _run(fake, config=_make_config(configured=False))
    assert outcome.success is False
    assert outcome.failure_reason == "admin_api_unconfigured"
    assert fake.calls == []


def test_factory_requires_params_and_valid_tier() -> None:
    cfg = _make_config()
    assert make_enforce_account_key_tier_handler(cfg, {}) is None
    assert make_enforce_account_key_tier_handler(
        cfg, {"account_key_id": _KEY, "tier": "admin"},  # invalid tier
    ) is None
    assert make_enforce_account_key_tier_handler(
        cfg, {"account_key_id": _KEY, "tier": "ro"},
    ) is not None
