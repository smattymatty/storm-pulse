"""Tests for stormpulse.garage.get_bucket_owners.

Read-only: which access keys own a bucket (BUCKETS-013 provenance). Storm
matches the ids to AccountKey rows for the bucket-detail "created with account
key X" line.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stormpulse.commands.jobs import JobOutcome
from stormpulse.config import GarageConfig
from stormpulse.garage.get_bucket_owners import (
    make_get_bucket_owners_handler,
    run_get_bucket_owners,
)

_BUCKET = "f1dc32249aa1d80a"


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


class _FakeAdmin:
    def __init__(self, info):
        self.info = info

    def get_bucket_info(self, **kw) -> tuple[dict[str, Any] | None, str]:
        return self.info


def _install(monkeypatch, info):
    fake = _FakeAdmin(info)
    monkeypatch.setattr(
        "stormpulse.garage.get_bucket_owners.admin_api.get_bucket_info",
        fake.get_bucket_info,
    )
    return fake


async def _run(*, config=None) -> JobOutcome:
    return await run_get_bucket_owners(
        progress=_Progress(), garage_config=config or _make_config(), bucket_id=_BUCKET,
    )


@pytest.mark.asyncio
async def test_returns_owner_key_ids(monkeypatch):
    _install(monkeypatch, ({"keys": [
        {"accessKeyId": "GKowner", "permissions": {"owner": True}},
        {"accessKeyId": "GKreader", "permissions": {"owner": False}},
    ]}, ""))
    outcome = await _run()
    assert outcome.success is True
    assert outcome.extras["owner_key_ids"] == ["GKowner"]


@pytest.mark.asyncio
async def test_bucket_gone_empty(monkeypatch):
    _install(monkeypatch, (None, "HTTP 404: NoSuchBucket"))
    outcome = await _run()
    assert outcome.success is True
    assert outcome.extras["owner_key_ids"] == []


@pytest.mark.asyncio
async def test_transient_failure(monkeypatch):
    _install(monkeypatch, (None, "HTTP 503: unavailable"))
    outcome = await _run()
    assert outcome.success is False
    assert outcome.failure_reason == "bucket_read_failed"


@pytest.mark.asyncio
async def test_unconfigured_fails(monkeypatch):
    _install(monkeypatch, ({"keys": []}, ""))
    outcome = await _run(config=_make_config(configured=False))
    assert outcome.success is False
    assert outcome.failure_reason == "admin_api_unconfigured"


def test_factory_requires_bucket_id():
    cfg = _make_config()
    assert make_get_bucket_owners_handler(cfg, {}) is None
    assert make_get_bucket_owners_handler(cfg, {"bucket_id": _BUCKET}) is not None
