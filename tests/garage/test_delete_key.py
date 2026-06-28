"""Tests for stormpulse.garage.delete_key.

Admin-API key delete that reports a structured confirmed-gone outcome
(ADR garage/001), backing the BUCKETS-013 credential-kill tombstone sweep.
The contract the sweep depends on:

  - 2xx          -> success, outcome="deleted",        confirmed_absent=True
  - positive 404 -> success, outcome="already_absent",  confirmed_absent=True
  - anything else -> success=False (transient; the key is NOT certified gone)

As in the other migrated handlers, we patch ``admin_api`` and assert on the
recorded calls.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stormpulse.commands.jobs import JobOutcome
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.delete_key import (
    make_delete_key_handler,
    run_delete_key,
)

_KEY_ID = "GK31c0b8a9f2e14d6c"


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
    ) -> None:
        self.events.append((stage, current, total, message))


class _FakeAdmin:
    def __init__(self, result: tuple[bool, str]) -> None:
        self.result = result
        self.calls: list[str] = []

    def delete_key(
        self, *, admin_url: str, admin_token: str, access_key_id: str,
    ) -> tuple[bool, str]:
        self.calls.append(access_key_id)
        return self.result

    # The handler reuses the real classifier; keep it honest in tests.
    @staticmethod
    def is_not_found(err: str) -> bool:
        from stormpulse.garage import admin_api
        return admin_api.is_not_found(err)


def _install(monkeypatch: pytest.MonkeyPatch, result: tuple[bool, str]) -> _FakeAdmin:
    fake = _FakeAdmin(result)
    monkeypatch.setattr(
        "stormpulse.garage.delete_key.admin_api.delete_key", fake.delete_key,
    )
    return fake


async def _run(
    *, config: GarageConfig | None = None, key_id: str = _KEY_ID,
) -> JobOutcome:
    return await run_delete_key(
        progress=_ProgressRecorder(),
        garage_config=config or _make_config(),
        key_id=key_id,
    )


@pytest.mark.asyncio
async def test_deleted_is_confirmed_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, (True, ""))
    outcome = await _run()

    assert outcome.success is True
    assert outcome.extras["outcome"] == "deleted"
    assert outcome.extras["confirmed_absent"] is True
    assert outcome.extras["key_id"] == _KEY_ID
    assert fake.calls == [_KEY_ID]


@pytest.mark.asyncio
async def test_already_absent_404_is_confirmed_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A positive 404 / NoSuchKey clears the tombstone just like a delete.
    fake = _install(monkeypatch, (False, "HTTP 404: NoSuchKey"))
    outcome = await _run()

    assert outcome.success is True
    assert outcome.extras["outcome"] == "already_absent"
    assert outcome.extras["confirmed_absent"] is True
    assert fake.calls == [_KEY_ID]


@pytest.mark.asyncio
async def test_transient_error_is_not_certified_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A 5xx must NOT certify the key as gone; the sweep keeps retrying.
    _install(monkeypatch, (False, "HTTP 500: server error"))
    outcome = await _run()

    assert outcome.success is False
    assert outcome.extras["outcome"] == "transient_error"
    assert outcome.extras["confirmed_absent"] is False
    assert outcome.failure_reason == "key_delete_failed"


@pytest.mark.asyncio
async def test_transport_error_is_not_certified_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, (False, "connection refused"))
    outcome = await _run()

    assert outcome.success is False
    assert outcome.extras["confirmed_absent"] is False


@pytest.mark.asyncio
async def test_unconfigured_admin_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch, (True, ""))
    outcome = await _run(config=_make_config(configured=False))

    assert outcome.success is False
    assert outcome.extras["confirmed_absent"] is False
    # Never reaches the admin call when unconfigured.
    assert fake.calls == []


def test_factory_requires_key_id() -> None:
    assert make_delete_key_handler(_make_config(), {}) is None
    assert make_delete_key_handler(_make_config(), {"key_id": ""}) is None
    assert make_delete_key_handler(_make_config(), {"key_id": _KEY_ID}) is not None
