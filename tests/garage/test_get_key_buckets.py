"""Tests for stormpulse.garage.jobs.get_key_buckets.

Read-only: return the buckets an account key owns. Powers
the dashboard's per-key bucket list and the revoke at-risk split, since Storm
does not store the key->bucket link.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stormpulse.commands.jobs import JobOutcome
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.jobs.get_key_buckets import (
    make_get_key_buckets_handler,
    run_get_key_buckets,
)

_KEY = "GKaccountkey00000"
_B1 = "b1" + "0" * 62
_B2 = "b2" + "0" * 62


def _make_config(*, configured: bool = True) -> GarageConfig:
    return GarageConfig(
        enabled=True, container_name="garaged", garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=Path("/opt/garage/garage.toml"),
        admin_url="http://127.0.0.1:3903" if configured else "",
        admin_token="tok" if configured else "",
    )


class _Progress:
    async def __call__(self, *a: Any, **k: Any) -> None:
        return None


def _bucket(full_id, *, owner, aliases=()):
    return {
        "id": full_id,
        "permissions": {"read": owner, "write": owner, "owner": owner},
        "localAliases": list(aliases),
    }


class _FakeAdmin:
    def __init__(self, key_info):
        self.key_info = key_info

    def get_key_info(self, **kw) -> tuple[dict[str, Any] | None, str]:
        return self.key_info


def _install(monkeypatch, key_info):
    fake = _FakeAdmin(key_info)
    monkeypatch.setattr(
        "stormpulse.garage.jobs.get_key_buckets.admin_api.get_key_info",
        fake.get_key_info,
    )
    return fake


async def _run(*, config=None) -> JobOutcome:
    return await run_get_key_buckets(
        progress=_Progress(), garage_config=config or _make_config(), key_id=_KEY,
    )


@pytest.mark.asyncio
async def test_returns_owned_buckets(monkeypatch):
    _install(monkeypatch, ({"buckets": [
        _bucket(_B1, owner=True, aliases=["vault"]),
        _bucket(_B2, owner=False),  # not owned -> excluded
    ]}, ""))
    outcome = await _run()
    assert outcome.success is True
    assert outcome.extras["owned_buckets"] == [{"id": _B1, "alias": "vault"}]


@pytest.mark.asyncio
async def test_key_gone_empty(monkeypatch):
    _install(monkeypatch, (None, "HTTP 404: NoSuchKey"))
    outcome = await _run()
    assert outcome.success is True
    assert outcome.extras["owned_buckets"] == []


@pytest.mark.asyncio
async def test_transient_failure(monkeypatch):
    _install(monkeypatch, (None, "HTTP 503: unavailable"))
    outcome = await _run()
    assert outcome.success is False
    assert outcome.failure_reason == "key_read_failed"


@pytest.mark.asyncio
async def test_unconfigured_fails(monkeypatch):
    _install(monkeypatch, ({"buckets": []}, ""))
    outcome = await _run(config=_make_config(configured=False))
    assert outcome.success is False
    assert outcome.failure_reason == "admin_api_unconfigured"


def test_factory_requires_key_id():
    cfg = _make_config()
    assert make_get_key_buckets_handler(cfg, {}) is None
    assert make_get_key_buckets_handler(cfg, {"key_id": _KEY}) is not None
