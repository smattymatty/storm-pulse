"""Tests for the BUCKETS-006 quota write: the admin-API client + the handler."""
from __future__ import annotations

import json
from typing import Any

import pytest

from stormpulse.garage import admin_api, set_quota
from stormpulse.garage.set_quota import make_set_quota_handler, run_set_quota

_ADMIN = {"admin_url": "http://127.0.0.1:3903", "admin_token": "tok"}


class _ProgressRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, int, int | None, str]] = []

    async def __call__(
        self, stage: str, current: int, total: int | None, message: str,
    ) -> None:
        self.events.append((stage, current, total, message))


# ---------------------------------------------------------------------------
# admin_api.set_bucket_quota - request shape + status handling
# ---------------------------------------------------------------------------


def test_set_bucket_quota_builds_updatebucket_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class _Resp:
        status = 200

        def read(self) -> bytes:
            return b"{}"

    class _Conn:
        def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
            captured["host"] = host
            captured["port"] = port

        def request(
            self,
            method: str,
            path: str,
            body: bytes | None = None,
            headers: dict[str, str] | None = None,
        ) -> None:
            captured.update(method=method, path=path, body=body, headers=headers)

        def getresponse(self) -> _Resp:
            return _Resp()

        def close(self) -> None:
            pass

    monkeypatch.setattr("http.client.HTTPConnection", _Conn)

    ok, err = admin_api.set_bucket_quota(
        admin_url="http://127.0.0.1:3903",
        admin_token="tok",
        bucket_id="8742c023e7e97dc8",
        max_size_bytes=200_000_000,
    )

    assert (ok, err) == (True, "")
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 3903
    assert captured["method"] == "POST"
    assert captured["path"] == "/v2/UpdateBucket?id=8742c023e7e97dc8"
    assert captured["headers"]["Authorization"] == "Bearer tok"
    assert json.loads(captured["body"]) == {
        "quotas": {"maxSize": 200_000_000, "maxObjects": None},
    }


def test_set_bucket_quota_returns_error_on_non_2xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Resp:
        status = 403

        def read(self) -> bytes:
            return b"forbidden"

    class _Conn:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        def request(self, *a: Any, **k: Any) -> None:
            pass

        def getresponse(self) -> _Resp:
            return _Resp()

        def close(self) -> None:
            pass

    monkeypatch.setattr("http.client.HTTPConnection", _Conn)

    ok, err = admin_api.set_bucket_quota(
        admin_url="http://127.0.0.1:3903",
        admin_token="tok",
        bucket_id="a" * 16,
        max_size_bytes=1,
    )
    assert ok is False
    assert "403" in err


# ---------------------------------------------------------------------------
# make_set_quota_handler - guards
# ---------------------------------------------------------------------------


def test_handler_none_when_admin_not_configured() -> None:
    handler = make_set_quota_handler(
        {"bucket_id": "a" * 16, "max_size": "1000"}, admin_url="", admin_token="",
    )
    assert handler is None


def test_handler_none_when_missing_params() -> None:
    assert make_set_quota_handler({"bucket_id": "a" * 16}, **_ADMIN) is None
    assert make_set_quota_handler({"max_size": "1000"}, **_ADMIN) is None


def test_handler_none_when_max_size_not_int() -> None:
    assert make_set_quota_handler(
        {"bucket_id": "a" * 16, "max_size": "lots"}, **_ADMIN,
    ) is None


def test_handler_built_when_valid() -> None:
    assert make_set_quota_handler(
        {"bucket_id": "a" * 16, "max_size": "1000"}, **_ADMIN,
    ) is not None


# ---------------------------------------------------------------------------
# run_set_quota - happy + failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_set_quota_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, str, int]] = []

    def fake(
        *, admin_url: str, admin_token: str, bucket_id: str, max_size_bytes: int,
    ) -> tuple[bool, str]:
        calls.append((admin_url, admin_token, bucket_id, max_size_bytes))
        return True, ""

    monkeypatch.setattr(admin_api, "set_bucket_quota", fake)

    outcome = await run_set_quota(
        _ProgressRecorder(),
        admin_url="http://127.0.0.1:3903",
        admin_token="tok",
        bucket_id="8742c023e7e97dc8",
        max_size_bytes=200_000_000,
    )
    assert outcome.success is True
    assert outcome.exit_code == 0
    assert calls == [("http://127.0.0.1:3903", "tok", "8742c023e7e97dc8", 200_000_000)]
    assert outcome.extras["max_size_bytes"] == 200_000_000


@pytest.mark.asyncio
async def test_run_set_quota_surfaces_admin_api_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake(**kw: Any) -> tuple[bool, str]:
        return False, "HTTP 403: forbidden"

    monkeypatch.setattr(admin_api, "set_bucket_quota", fake)

    outcome = await run_set_quota(
        _ProgressRecorder(),
        admin_url="http://127.0.0.1:3903",
        admin_token="tok",
        bucket_id="8742c023e7e97dc8",
        max_size_bytes=200_000_000,
    )
    assert outcome.success is False
    assert "403" in outcome.stderr
    assert outcome.failure_reason == "os_error"
