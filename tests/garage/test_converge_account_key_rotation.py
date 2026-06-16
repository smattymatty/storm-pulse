"""Tests for stormpulse.garage.converge_account_key_rotation.

One idempotent pass that grants the new account key owner + alias on every
bucket the old key owns that the new key does not. The contract the Storm-side
convergence loop depends on:

  - a pass with nothing left to transfer -> converged=True
  - a pass that transfers buckets       -> converged=False (re-dispatch to confirm)
  - old key already gone (404)          -> converged=True
  - new key unreadable                  -> failure (retry next tick)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stormpulse.commands.jobs import JobOutcome
from stormpulse.config import GarageConfig
from stormpulse.garage.converge_account_key_rotation import (
    make_converge_account_key_rotation_handler,
    run_converge_account_key_rotation,
)

_OLD = "GKoldaccountkey00"
_NEW = "GKnewaccountkey00"
_B1 = "b1" + "0" * 62
_B2 = "b2" + "0" * 62


def _make_config(*, configured: bool = True) -> GarageConfig:
    return GarageConfig(
        enabled=True, container_name="garaged", garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=Path("/opt/garage/garage.toml"),
        state_push_interval_seconds=300,
        admin_url="http://127.0.0.1:3903" if configured else "",
        admin_token="tok" if configured else "",
    )


class _Progress:
    async def __call__(self, *a) -> None:
        return None


def _bucket(full_id: str, *, owner: bool, aliases=()) -> dict[str, Any]:
    return {
        "id": full_id,
        "permissions": {"read": owner, "write": owner, "owner": owner},
        "localAliases": list(aliases),
    }


class _FakeAdmin:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # key id -> (info|None, err)
        self.key_info: dict[str, tuple[dict[str, Any] | None, str]] = {}
        self.allow_result: tuple[bool, str] = (True, "")

    def get_key_info(self, *, admin_url, admin_token, access_key_id):
        self.calls.append(("get_key_info", {"id": access_key_id}))
        return self.key_info.get(access_key_id, ({"buckets": []}, ""))

    def allow_bucket_key(self, *, admin_url, admin_token, bucket_ref,
                         access_key_id, read, write, owner):
        self.calls.append(("allow_bucket_key", {
            "bucket_ref": bucket_ref, "read": read, "write": write, "owner": owner,
        }))
        return self.allow_result

    def add_bucket_alias_local(self, *, admin_url, admin_token, bucket_ref,
                               access_key_id, local_alias):
        self.calls.append(("add_bucket_alias_local", {
            "bucket_ref": bucket_ref, "local_alias": local_alias,
        }))
        return (True, "")

    def ops(self):
        return [op for op, _ in self.calls]


def _install(monkeypatch):
    fake = _FakeAdmin()
    for name in ("get_key_info", "allow_bucket_key", "add_bucket_alias_local"):
        monkeypatch.setattr(
            f"stormpulse.garage.converge_account_key_rotation.admin_api.{name}",
            getattr(fake, name),
        )
    return fake


async def _run(fake, *, config=None) -> JobOutcome:
    return await run_converge_account_key_rotation(
        progress=_Progress(), garage_config=config or _make_config(),
        old_key_id=_OLD, new_key_id=_NEW,
    )


@pytest.mark.asyncio
async def test_converge_from_snapshot_skips_old_read(monkeypatch) -> None:
    # Leak path: the old key is dead, so converge from the captured snapshot
    # and never read the old key (it would 404).
    fake = _install(monkeypatch)
    fake.key_info = {_NEW: ({"buckets": []}, "")}  # only new is read
    outcome = await run_converge_account_key_rotation(
        progress=_Progress(), garage_config=_make_config(),
        old_key_id=_OLD, new_key_id=_NEW,
        bucket_snapshot=[{"id": _B1, "alias": "vault"}],
    )
    assert outcome.success is True
    assert outcome.extras["transferred"] == [_B1[:16]]
    # The dead old key is never read.
    assert all(c[1]["id"] != _OLD for c in fake.calls if c[0] == "get_key_info")
    assert ("add_bucket_alias_local", {"bucket_ref": _B1, "local_alias": "vault"}) in fake.calls


def _graded(full_id, *, read, write, owner, aliases=()):
    return {
        "id": full_id,
        "permissions": {"read": read, "write": write, "owner": owner},
        "localAliases": list(aliases),
    }


@pytest.mark.asyncio
async def test_transfers_non_owner_grant_at_its_tier(monkeypatch) -> None:
    # BUCKETS-014: an rw attach (not owner) must transfer AS rw, not silently
    # die under the old owner-only filter, and not get upgraded to owner.
    fake = _install(monkeypatch)
    fake.key_info = {
        _OLD: ({"buckets": [_graded(_B1, read=True, write=True, owner=False)]}, ""),
        _NEW: ({"buckets": []}, ""),
    }
    outcome = await _run(fake)
    assert outcome.success is True
    assert outcome.extras["transferred"] == [_B1[:16]]
    grant = next(c[1] for c in fake.calls if c[0] == "allow_bucket_key")
    assert grant["read"] is True and grant["write"] is True and grant["owner"] is False


@pytest.mark.asyncio
async def test_new_lower_tier_is_not_covered(monkeypatch) -> None:
    # old owns (owner); new only has rw -> not covered, must transfer owner.
    fake = _install(monkeypatch)
    fake.key_info = {
        _OLD: ({"buckets": [_bucket(_B1, owner=True)]}, ""),
        _NEW: ({"buckets": [_graded(_B1, read=True, write=True, owner=False)]}, ""),
    }
    outcome = await _run(fake)
    assert outcome.extras["converged"] is False
    grant = next(c[1] for c in fake.calls if c[0] == "allow_bucket_key")
    assert grant["owner"] is True


@pytest.mark.asyncio
async def test_already_converged(monkeypatch) -> None:
    fake = _install(monkeypatch)
    # Old owns B1; new already owns B1 too -> nothing to transfer.
    fake.key_info = {
        _OLD: ({"buckets": [_bucket(_B1, owner=True)]}, ""),
        _NEW: ({"buckets": [_bucket(_B1, owner=True)]}, ""),
    }
    outcome = await _run(fake)
    assert outcome.success is True
    assert outcome.extras["converged"] is True
    assert "allow_bucket_key" not in fake.ops()


@pytest.mark.asyncio
async def test_transfers_pending_buckets(monkeypatch) -> None:
    fake = _install(monkeypatch)
    # Old owns B1 + B2; new owns neither -> transfer both, not yet converged.
    fake.key_info = {
        _OLD: ({"buckets": [
            _bucket(_B1, owner=True, aliases=["vault"]),
            _bucket(_B2, owner=True),
        ]}, ""),
        _NEW: ({"buckets": []}, ""),
    }
    outcome = await _run(fake)
    assert outcome.success is True
    assert outcome.extras["converged"] is False
    assert set(outcome.extras["transferred"]) == {_B1[:16], _B2[:16]}
    grants = [c for c in fake.calls if c[0] == "allow_bucket_key"]
    assert all(g[1]["owner"] is True for g in grants)
    # B1 had an alias; it is replicated on the new key. B2 had none.
    assert ("add_bucket_alias_local", {"bucket_ref": _B1, "local_alias": "vault"}) in fake.calls


@pytest.mark.asyncio
async def test_only_owned_buckets_transfer(monkeypatch) -> None:
    fake = _install(monkeypatch)
    # Old has a non-owner grant on B2; only owned buckets are transferred.
    fake.key_info = {
        _OLD: ({"buckets": [
            _bucket(_B1, owner=True),
            _bucket(_B2, owner=False),
        ]}, ""),
        _NEW: ({"buckets": []}, ""),
    }
    outcome = await _run(fake)
    assert outcome.extras["transferred"] == [_B1[:16]]


@pytest.mark.asyncio
async def test_old_key_gone_is_converged(monkeypatch) -> None:
    fake = _install(monkeypatch)
    fake.key_info = {_OLD: (None, "HTTP 404: NoSuchKey")}
    outcome = await _run(fake)
    assert outcome.success is True
    assert outcome.extras["converged"] is True


@pytest.mark.asyncio
async def test_new_key_unreadable_fails(monkeypatch) -> None:
    fake = _install(monkeypatch)
    fake.key_info = {
        _OLD: ({"buckets": [_bucket(_B1, owner=True)]}, ""),
        _NEW: (None, "HTTP 503: unavailable"),
    }
    outcome = await _run(fake)
    assert outcome.success is False
    assert outcome.failure_reason == "new_key_read_failed"


@pytest.mark.asyncio
async def test_grant_failure_flags_cleanup(monkeypatch) -> None:
    fake = _install(monkeypatch)
    fake.key_info = {
        _OLD: ({"buckets": [_bucket(_B1, owner=True)]}, ""),
        _NEW: ({"buckets": []}, ""),
    }
    fake.allow_result = (False, "boom")
    outcome = await _run(fake)
    assert outcome.success is True
    assert outcome.extras["transferred"] == []
    assert outcome.extras["manual_cleanup_required"][0]["bucket_id"] == _B1[:16]


@pytest.mark.asyncio
async def test_unconfigured_admin_fails(monkeypatch) -> None:
    fake = _install(monkeypatch)
    outcome = await _run(fake, config=_make_config(configured=False))
    assert outcome.success is False
    assert outcome.failure_reason == "admin_api_unconfigured"
    assert fake.calls == []


def test_factory_requires_params() -> None:
    cfg = _make_config()
    assert make_converge_account_key_rotation_handler(cfg, {}) is None
    assert make_converge_account_key_rotation_handler(cfg, {"old_key_id": _OLD}) is None
    assert make_converge_account_key_rotation_handler(
        cfg, {"old_key_id": _OLD, "new_key_id": _NEW},
    ) is not None
