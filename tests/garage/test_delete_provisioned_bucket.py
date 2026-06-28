"""Tests for stormpulse.garage.delete_provisioned_bucket.

Deletes an empty bucket via the admin HTTP API (ADR garage/001):

  1. GetBucketInfo (idempotent on a missing bucket; refuses a non-empty one)
  2. DeleteBucket by id (removes the bucket and ALL its aliases atomically)
  3. best-effort cleanup of keys the deletion left unmoored

There is no rollback (DeleteBucket is the only mutation). As in the other
migrated handlers, we patch the ``admin_api`` functions and assert on the
recorded calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stormpulse.commands.jobs import JobOutcome
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.delete_provisioned_bucket import (
    make_delete_provisioned_bucket_handler,
    run_delete_provisioned_bucket,
)

_PREFIX = "f1dc32249aa1d80a"  # Storm's 16-char garage_bucket_id
_FULL_ID = _PREFIX + "0" * 48


def _make_config() -> GarageConfig:
    return GarageConfig(
        enabled=True,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=Path("/opt/garage/garage.toml"),
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
    """Records admin_api calls and returns configurable canned results."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        # Defaults: an empty bucket with no keys, deletes cleanly.
        self.bucket_info: tuple[dict[str, Any] | None, str] = (
            {"id": _FULL_ID, "objects": 0, "keys": []},
            "",
        )
        self.delete_bucket_result: tuple[bool, str] = (True, "")
        self.key_info: dict[str, tuple[dict[str, Any] | None, str]] = {}
        self.delete_key_result: dict[str, tuple[bool, str]] = {}

    def get_bucket_info(
        self, *, admin_url: str, admin_token: str, bucket_ref: str,
    ) -> tuple[dict[str, Any] | None, str]:
        self.calls.append(("get_bucket_info", {"bucket_ref": bucket_ref}))
        return self.bucket_info

    def delete_bucket(
        self, *, admin_url: str, admin_token: str, bucket_ref: str,
    ) -> tuple[bool, str]:
        self.calls.append(("delete_bucket", {"bucket_ref": bucket_ref}))
        return self.delete_bucket_result

    def get_key_info(
        self, *, admin_url: str, admin_token: str, access_key_id: str,
    ) -> tuple[dict[str, Any] | None, str]:
        self.calls.append(("get_key_info", {"access_key_id": access_key_id}))
        return self.key_info.get(
            access_key_id, ({"accessKeyId": access_key_id, "buckets": []}, ""),
        )

    def delete_key(
        self, *, admin_url: str, admin_token: str, access_key_id: str,
    ) -> tuple[bool, str]:
        self.calls.append(("delete_key", {"access_key_id": access_key_id}))
        return self.delete_key_result.get(access_key_id, (True, ""))

    def ops(self) -> list[str]:
        return [op for op, _ in self.calls]


def _install(monkeypatch: pytest.MonkeyPatch) -> _FakeAdmin:
    fake = _FakeAdmin()
    for name in ("get_bucket_info", "delete_bucket", "get_key_info", "delete_key"):
        monkeypatch.setattr(
            f"stormpulse.garage.delete_provisioned_bucket.admin_api.{name}",
            getattr(fake, name),
        )
    return fake


async def _run(
    fake: _FakeAdmin, *, config: GarageConfig | None = None,
) -> JobOutcome:
    return await run_delete_provisioned_bucket(
        progress=_ProgressRecorder(),
        garage_config=config or _make_config(),
        bucket_id=_PREFIX,
    )


def _info(*, objects: int = 0, key_ids: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "id": _FULL_ID,
        "objects": objects,
        "keys": [{"accessKeyId": k} for k in key_ids],
    }


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_no_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch)
    outcome = await _run(fake)

    assert outcome.success is True
    assert outcome.extras["bucket_id"] == _PREFIX
    assert outcome.extras["step_completed"] == "key_cleanup"
    assert outcome.extras["rollback_status"] == "not_required"
    assert outcome.extras["manual_cleanup_required"] == []
    assert outcome.extras["keys_deleted"] == []
    assert outcome.extras["keys_skipped"] == []
    assert fake.ops() == ["get_bucket_info", "delete_bucket"]
    # DeleteBucket is addressed by the full id from GetBucketInfo.
    assert fake.calls[1][1] == {"bucket_ref": _FULL_ID}


@pytest.mark.asyncio
async def test_unmoored_key_is_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch)
    fake.bucket_info = (_info(key_ids=("GKa",)), "")
    fake.key_info = {"GKa": ({"accessKeyId": "GKa", "buckets": []}, "")}

    outcome = await _run(fake)

    assert outcome.success is True
    assert outcome.extras["keys_deleted"] == ["GKa"]
    assert outcome.extras["keys_skipped"] == []
    assert fake.ops() == [
        "get_bucket_info", "delete_bucket", "get_key_info", "delete_key",
    ]
    assert fake.calls[-1][1] == {"access_key_id": "GKa"}


@pytest.mark.asyncio
async def test_shared_key_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch)
    fake.bucket_info = (_info(key_ids=("GKa",)), "")
    fake.key_info = {"GKa": ({"accessKeyId": "GKa", "buckets": [{"id": "other"}]}, "")}

    outcome = await _run(fake)

    assert outcome.success is True
    assert outcome.extras["keys_skipped"] == ["GKa"]
    assert outcome.extras["keys_deleted"] == []
    # No delete_key for a key still attached elsewhere.
    assert "delete_key" not in fake.ops()


@pytest.mark.asyncio
async def test_already_gone_key_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch)
    fake.bucket_info = (_info(key_ids=("GKa",)), "")
    fake.key_info = {"GKa": (None, "HTTP 404: NoSuchKey")}

    outcome = await _run(fake)

    assert outcome.success is True
    assert outcome.extras["keys_skipped"] == ["GKa"]
    assert outcome.extras["manual_cleanup_required"] == []
    assert "delete_key" not in fake.ops()


@pytest.mark.asyncio
async def test_key_info_error_flags_manual_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch)
    fake.bucket_info = (_info(key_ids=("GKa",)), "")
    fake.key_info = {"GKa": (None, "HTTP 500: server error")}

    outcome = await _run(fake)

    assert outcome.success is True
    assert outcome.extras["manual_cleanup_required"] == [{"type": "key", "id": "GKa"}]


@pytest.mark.asyncio
async def test_key_delete_failure_flags_manual_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch)
    fake.bucket_info = (_info(key_ids=("GKa",)), "")
    fake.delete_key_result = {"GKa": (False, "HTTP 500")}

    outcome = await _run(fake)

    assert outcome.success is True
    assert outcome.extras["keys_deleted"] == []
    assert outcome.extras["manual_cleanup_required"] == [{"type": "key", "id": "GKa"}]


# ---------------------------------------------------------------------------
# Idempotence + failure points
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_already_gone_is_idempotent_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch)
    fake.bucket_info = (None, "HTTP 404: NoSuchBucket")

    outcome = await _run(fake)

    assert outcome.success is True
    assert outcome.extras["already_absent"] is True
    assert outcome.extras["step_completed"] == "bucket_info"
    assert fake.ops() == ["get_bucket_info"]  # no delete attempted


@pytest.mark.asyncio
async def test_bucket_info_error_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch)
    fake.bucket_info = (None, "HTTP 500: server error")

    outcome = await _run(fake)

    assert outcome.success is False
    assert outcome.failure_reason == "bucket_info_failed"
    assert outcome.extras["step_failed"] == "bucket_info"
    assert fake.ops() == ["get_bucket_info"]


@pytest.mark.asyncio
async def test_non_empty_bucket_refused_before_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch)
    fake.bucket_info = (_info(objects=3), "")

    outcome = await _run(fake)

    assert outcome.success is False
    assert outcome.failure_reason == "bucket_not_empty"
    assert outcome.extras["step_failed"] == "bucket_info"
    assert fake.ops() == ["get_bucket_info"]  # no delete attempted


@pytest.mark.asyncio
async def test_delete_bucket_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch)
    fake.delete_bucket_result = (False, "HTTP 500: boom")

    outcome = await _run(fake)

    assert outcome.success is False
    assert outcome.failure_reason == "bucket_delete_failed"
    assert outcome.extras["step_failed"] == "bucket_delete"
    assert outcome.extras["step_completed"] == "bucket_info"


@pytest.mark.asyncio
async def test_delete_bucket_not_empty_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch)
    fake.delete_bucket_result = (False, "HTTP 400: Bucket is not empty")

    outcome = await _run(fake)

    assert outcome.success is False
    assert outcome.failure_reason == "bucket_not_empty"
    assert outcome.extras["step_failed"] == "bucket_delete"


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
    assert fake.ops() == []


# ---------------------------------------------------------------------------
# Handler factory
# ---------------------------------------------------------------------------


def test_handler_factory_returns_none_on_missing_bucket_id() -> None:
    handler = make_delete_provisioned_bucket_handler(_make_config(), params={})
    assert handler is None


def test_handler_factory_returns_handler_when_complete() -> None:
    handler = make_delete_provisioned_bucket_handler(
        _make_config(), params={"bucket_id": _PREFIX},
    )
    assert handler is not None
    assert callable(handler)
