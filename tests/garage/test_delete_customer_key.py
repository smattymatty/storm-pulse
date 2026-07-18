"""Tests for stormpulse.garage.jobs.delete_customer_key.

Guarded delete of a per-bucket customer key: coverage is verified against
live Garage state (GetBucketInfo) before the delete fires. The contract:

  - a covering key holds a live grant -> delete, confirmed-gone semantics
    (deleted / already_absent -> success)
  - no covering key holds a live grant -> ``not_covered`` abort, NO delete
  - all-denied (detached-shaped) grants are presence, not coverage
  - bucket positively 404 -> vacuous coverage (nothing left to protect),
    delete proceeds, ``bucket_absent`` reported
  - GetBucketInfo transient error -> fail closed, NO delete
  - delete transient error after the guard passed -> failure with
    ``guard_passed`` True, so the caller may finish the kill unconditionally

As in the other migrated handlers, we patch ``admin_api`` and assert on the
recorded calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stormpulse.commands.jobs import JobOutcome
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.jobs.delete_customer_key import (
    make_delete_customer_key_handler,
    run_delete_customer_key,
)

_KEY_ID = "GK31c0b8a9f2e14d6c"
_BUCKET_ID = "a" * 64
_ACCOUNT_KEY = "GKaccount111111111"
_OTHER_KEY = "GKother22222222222"


def _make_config(*, configured: bool = True) -> GarageConfig:
    return GarageConfig(
        enabled=True,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=Path("/opt/garage/garage.toml"),
        admin_url="http://127.0.0.1:3903" if configured else "",
        admin_token="tok" if configured else "",
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


def _grant(
    key_id: str, *, read: bool = False, write: bool = False, owner: bool = False,
) -> dict[str, Any]:
    return {
        "accessKeyId": key_id,
        "permissions": {"read": read, "write": write, "owner": owner},
    }


class _FakeAdmin:
    """Records calls; serves a canned bucket-info and delete result."""

    def __init__(
        self,
        bucket_info: tuple[dict[str, Any] | None, str],
        delete_result: tuple[bool, str] = (True, ""),
    ) -> None:
        self.bucket_info = bucket_info
        self.delete_result = delete_result
        self.info_calls: list[str] = []
        self.delete_calls: list[str] = []

    def get_bucket_info(
        self, *, admin_url: str, admin_token: str, bucket_ref: str,
    ) -> tuple[dict[str, Any] | None, str]:
        self.info_calls.append(bucket_ref)
        return self.bucket_info

    def delete_key(
        self, *, admin_url: str, admin_token: str, access_key_id: str,
    ) -> tuple[bool, str]:
        self.delete_calls.append(access_key_id)
        return self.delete_result


def _install(monkeypatch: pytest.MonkeyPatch, fake: _FakeAdmin) -> _FakeAdmin:
    base = "stormpulse.garage.jobs.delete_customer_key.admin_api."
    monkeypatch.setattr(base + "get_bucket_info", fake.get_bucket_info)
    monkeypatch.setattr(base + "delete_key", fake.delete_key)
    return fake


async def _run(
    *,
    config: GarageConfig | None = None,
    covering: list[str] | None = None,
) -> JobOutcome:
    return await run_delete_customer_key(
        progress=_ProgressRecorder(),
        garage_config=config or _make_config(),
        key_id=_KEY_ID,
        bucket_id=_BUCKET_ID,
        covering_key_ids=covering if covering is not None else [_ACCOUNT_KEY],
    )


@pytest.mark.asyncio
async def test_covered_deletes_confirmed_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, _FakeAdmin(
        ({"keys": [_grant(_ACCOUNT_KEY, read=True, write=True)]}, ""),
        (True, ""),
    ))
    outcome = await _run()

    assert outcome.success is True
    assert outcome.extras["outcome"] == "deleted"
    assert outcome.extras["confirmed_absent"] is True
    assert outcome.extras["covered_by"] == _ACCOUNT_KEY
    assert fake.info_calls == [_BUCKET_ID]
    assert fake.delete_calls == [_KEY_ID]


@pytest.mark.asyncio
async def test_not_covered_aborts_without_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Another key exists, but it is not on the covering list: abort, no delete.
    fake = _install(monkeypatch, _FakeAdmin(
        ({"keys": [_grant(_OTHER_KEY, read=True, write=True, owner=True)]}, ""),
    ))
    outcome = await _run()

    assert outcome.success is False
    assert outcome.failure_reason == "not_covered"
    assert outcome.extras["guard_passed"] is False
    assert fake.delete_calls == []


@pytest.mark.asyncio
async def test_all_denied_grant_is_presence_not_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A detached key can linger in the list with every permission False.
    fake = _install(monkeypatch, _FakeAdmin(
        ({"keys": [_grant(_ACCOUNT_KEY)]}, ""),
    ))
    outcome = await _run()

    assert outcome.success is False
    assert outcome.failure_reason == "not_covered"
    assert fake.delete_calls == []


@pytest.mark.asyncio
async def test_read_only_grant_is_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    # Any live permission counts; the control plane decides which keys qualify.
    fake = _install(monkeypatch, _FakeAdmin(
        ({"keys": [_grant(_ACCOUNT_KEY, read=True)]}, ""),
        (True, ""),
    ))
    outcome = await _run()

    assert outcome.success is True
    assert fake.delete_calls == [_KEY_ID]


@pytest.mark.asyncio
async def test_absent_bucket_is_vacuous_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Positive 404 on the bucket: nothing left to protect, delete proceeds.
    fake = _install(monkeypatch, _FakeAdmin(
        (None, "HTTP 404: no bucket found"),
        (True, ""),
    ))
    outcome = await _run()

    assert outcome.success is True
    assert outcome.extras["bucket_absent"] is True
    assert outcome.extras["covered_by"] == ""
    assert fake.delete_calls == [_KEY_ID]


@pytest.mark.asyncio
async def test_coverage_read_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A transient GetBucketInfo error must NOT let the delete proceed.
    fake = _install(monkeypatch, _FakeAdmin(
        (None, "HTTP 500: server error"),
    ))
    outcome = await _run()

    assert outcome.success is False
    assert outcome.failure_reason == "coverage_check_failed"
    assert outcome.extras["guard_passed"] is False
    assert fake.delete_calls == []


@pytest.mark.asyncio
async def test_already_absent_key_after_guard_is_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch, _FakeAdmin(
        ({"keys": [_grant(_ACCOUNT_KEY, write=True)]}, ""),
        (False, "HTTP 404: NoSuchKey"),
    ))
    outcome = await _run()

    assert outcome.success is True
    assert outcome.extras["outcome"] == "already_absent"
    assert outcome.extras["confirmed_absent"] is True
    assert fake.delete_calls == [_KEY_ID]


@pytest.mark.asyncio
async def test_delete_transient_after_guard_reports_guard_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 5xx on the delete after coverage passed: not certified gone, but the
    # caller learns the guard was satisfied at decision time.
    fake = _install(monkeypatch, _FakeAdmin(
        ({"keys": [_grant(_ACCOUNT_KEY, owner=True)]}, ""),
        (False, "HTTP 500: server error"),
    ))
    outcome = await _run()

    assert outcome.success is False
    assert outcome.failure_reason == "key_delete_failed"
    assert outcome.extras["guard_passed"] is True
    assert outcome.extras["confirmed_absent"] is False


@pytest.mark.asyncio
async def test_unconfigured_admin_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch, _FakeAdmin(({"keys": []}, "")))
    outcome = await _run(config=_make_config(configured=False))

    assert outcome.success is False
    assert fake.info_calls == []
    assert fake.delete_calls == []


def test_factory_requires_all_params() -> None:
    cfg = _make_config()
    good = {
        "key_id": _KEY_ID,
        "bucket_id": _BUCKET_ID,
        "covering_key_ids": f"{_ACCOUNT_KEY},{_OTHER_KEY}",
    }
    assert make_delete_customer_key_handler(cfg, good) is not None
    for missing in ("key_id", "bucket_id", "covering_key_ids"):
        broken = {k: v for k, v in good.items() if k != missing}
        assert make_delete_customer_key_handler(cfg, broken) is None
