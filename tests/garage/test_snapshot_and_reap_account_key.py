"""Tests for stormpulse.garage.snapshot_and_reap_account_key.

Leak-rotate kill (BUCKETS-013): snapshot the old key's owned buckets, THEN
delete the key object. The contract Storm's leak flow depends on:

  - snapshot is captured BEFORE the delete (so it survives the kill)
  - only owned buckets are snapshotted
  - delete is the key-object delete; success means the key is gone
  - old already gone (404) -> empty snapshot, success (manual-reclaim fallback)
  - a failed delete -> failure (do NOT certify gone; retry)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stormpulse.commands.jobs import JobOutcome
from stormpulse.config import GarageConfig
from stormpulse.garage.snapshot_and_reap_account_key import (
    make_snapshot_and_reap_account_key_handler,
    run_snapshot_and_reap_account_key,
)

_OLD = "GKcompromised0000"
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


def _bucket(full_id, *, owner, aliases=()):
    return {
        "id": full_id,
        "permissions": {"read": owner, "write": owner, "owner": owner},
        "localAliases": list(aliases),
    }


class _FakeAdmin:
    def __init__(self):
        self.calls: list[str] = []
        self.key_info: tuple[dict[str, Any] | None, str] = ({"buckets": []}, "")
        self.delete_result: tuple[bool, str] = (True, "")

    def get_key_info(self, **kw):
        self.calls.append("get_key_info")
        return self.key_info

    def delete_key(self, **kw):
        self.calls.append("delete_key")
        return self.delete_result


def _install(monkeypatch):
    fake = _FakeAdmin()
    for name in ("get_key_info", "delete_key"):
        monkeypatch.setattr(
            f"stormpulse.garage.snapshot_and_reap_account_key.admin_api.{name}",
            getattr(fake, name),
        )
    return fake


async def _run(fake, *, config=None) -> JobOutcome:
    return await run_snapshot_and_reap_account_key(
        progress=_Progress(), garage_config=config or _make_config(), old_key_id=_OLD,
    )


@pytest.mark.asyncio
async def test_snapshots_owned_then_reaps(monkeypatch):
    fake = _install(monkeypatch)
    fake.key_info = ({"buckets": [
        _bucket(_B1, owner=True, aliases=["vault"]),
        _bucket(_B2, owner=False),  # not owned -> excluded
    ]}, "")
    outcome = await _run(fake)
    assert outcome.success is True
    assert outcome.extras["reaped"] is True
    assert outcome.extras["snapshot"] == [{"id": _B1, "alias": "vault"}]
    # Snapshot read happens BEFORE the delete.
    assert fake.calls == ["get_key_info", "delete_key"]


@pytest.mark.asyncio
async def test_already_gone_empty_snapshot(monkeypatch):
    fake = _install(monkeypatch)
    fake.key_info = (None, "HTTP 404: NoSuchKey")
    outcome = await _run(fake)
    assert outcome.success is True
    assert outcome.extras["snapshot"] == []
    assert "delete_key" not in fake.calls


@pytest.mark.asyncio
async def test_delete_404_is_idempotent_success(monkeypatch):
    fake = _install(monkeypatch)
    fake.key_info = ({"buckets": [_bucket(_B1, owner=True)]}, "")
    fake.delete_result = (False, "HTTP 404: NoSuchKey")
    outcome = await _run(fake)
    assert outcome.success is True
    assert outcome.extras["reaped"] is True


@pytest.mark.asyncio
async def test_delete_transient_failure(monkeypatch):
    fake = _install(monkeypatch)
    fake.key_info = ({"buckets": [_bucket(_B1, owner=True)]}, "")
    fake.delete_result = (False, "HTTP 503: unavailable")
    outcome = await _run(fake)
    assert outcome.success is False
    assert outcome.failure_reason == "reap_failed"
    assert outcome.extras["reaped"] is False


@pytest.mark.asyncio
async def test_snapshot_read_transient_failure(monkeypatch):
    fake = _install(monkeypatch)
    fake.key_info = (None, "HTTP 503: unavailable")
    outcome = await _run(fake)
    assert outcome.success is False
    assert outcome.failure_reason == "snapshot_read_failed"
    assert "delete_key" not in fake.calls


@pytest.mark.asyncio
async def test_unconfigured_admin_fails(monkeypatch):
    fake = _install(monkeypatch)
    outcome = await _run(fake, config=_make_config(configured=False))
    assert outcome.success is False
    assert outcome.failure_reason == "admin_api_unconfigured"
    assert fake.calls == []


def test_factory_requires_old_key_id():
    cfg = _make_config()
    assert make_snapshot_and_reap_account_key_handler(cfg, {}) is None
    assert make_snapshot_and_reap_account_key_handler(
        cfg, {"old_key_id": _OLD},
    ) is not None
