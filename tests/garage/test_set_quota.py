"""Tests for the quota write: the admin-API client + the handler."""
from __future__ import annotations

import json
from typing import Any

import pytest

from stormpulse.garage import admin_api
from stormpulse.garage.jobs import set_quota
from stormpulse.garage.jobs.set_quota import make_set_quota_handler, run_set_quota

_ADMIN = {"admin_url": "http://127.0.0.1:3903", "admin_token": "tok"}
_PREFIX = "8742c023e7e97dc8"  # Storm's 16-char garage_bucket_id
_FULL_ID = _PREFIX + "efbd1b9371e8148cea3ad02f12dc5bd75dc91c92e0faaa56"  # 64 chars


class _ProgressRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, int, int | None, str]] = []

    async def __call__(
        self, stage: str, current: int, total: int | None, message: str,
    ) -> None:
        self.events.append((stage, current, total, message))


def _install_fake_admin(
    monkeypatch: pytest.MonkeyPatch,
    *,
    search_status: int = 200,
    search_body: bytes | None = None,
    update_status: int = 200,
) -> list[dict[str, Any]]:
    """Patch http.client.HTTPConnection with a fake that answers GetBucketInfo
    (resolve) and UpdateBucket (write). Returns the recorded request log."""
    requests: list[dict[str, Any]] = []
    body = json.dumps({"id": _FULL_ID}).encode() if search_body is None else search_body

    class _Resp:
        def __init__(self, status: int, payload: bytes) -> None:
            self.status = status
            self._payload = payload

        def read(self) -> bytes:
            return self._payload

    class _Conn:
        def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
            self._path = ""

        def request(
            self,
            method: str,
            path: str,
            body: bytes | None = None,
            headers: dict[str, str] | None = None,
        ) -> None:
            requests.append({"method": method, "path": path, "body": body, "headers": headers})
            self._path = path

        def getresponse(self) -> _Resp:
            if "GetBucketInfo" in self._path:
                return _Resp(search_status, body)
            return _Resp(update_status, b"{}")

        def close(self) -> None:
            pass

    monkeypatch.setattr("http.client.HTTPConnection", _Conn)
    return requests


# ---------------------------------------------------------------------------
# admin_api.set_bucket_quota - resolve-then-update
# ---------------------------------------------------------------------------


def test_resolves_prefix_then_updates_with_full_id(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _install_fake_admin(monkeypatch)
    ok, err = admin_api.set_bucket_quota(
        admin_url="http://127.0.0.1:3903",
        admin_token="tok",
        bucket_id=_PREFIX,
        max_size_bytes=200_000_000,
    )
    assert (ok, err) == (True, "")
    # First a GetBucketInfo search on the prefix, then UpdateBucket on the full id.
    assert requests[0]["method"] == "GET"
    assert f"search={_PREFIX}" in requests[0]["path"]
    assert requests[1]["method"] == "POST"
    assert requests[1]["path"] == f"/v2/UpdateBucket?id={_FULL_ID}"
    assert requests[1]["headers"]["Authorization"] == "Bearer tok"
    assert json.loads(requests[1]["body"]) == {
        "quotas": {"maxSize": 200_000_000, "maxObjects": None},
    }


def test_full_id_passes_straight_through(monkeypatch: pytest.MonkeyPatch) -> None:
    requests = _install_fake_admin(monkeypatch)
    ok, _ = admin_api.set_bucket_quota(
        admin_url="http://127.0.0.1:3903",
        admin_token="tok",
        bucket_id=_FULL_ID,
        max_size_bytes=1,
    )
    assert ok is True
    assert len(requests) == 1  # no resolution call
    assert requests[0]["path"] == f"/v2/UpdateBucket?id={_FULL_ID}"


def test_errors_when_prefix_does_not_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    # search returns a bucket whose id does NOT start with the prefix.
    _install_fake_admin(monkeypatch, search_body=json.dumps({"id": "f" * 64}).encode())
    ok, err = admin_api.set_bucket_quota(
        admin_url="http://127.0.0.1:3903",
        admin_token="tok",
        bucket_id=_PREFIX,
        max_size_bytes=1,
    )
    assert ok is False
    assert "no bucket matched" in err


def test_errors_on_update_non_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_admin(monkeypatch, update_status=403)
    ok, err = admin_api.set_bucket_quota(
        admin_url="http://127.0.0.1:3903",
        admin_token="tok",
        bucket_id=_PREFIX,
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
        bucket_id=_PREFIX,
        max_size_bytes=200_000_000,
    )
    assert outcome.success is True
    assert outcome.exit_code == 0
    assert calls == [("http://127.0.0.1:3903", "tok", _PREFIX, 200_000_000)]
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
        bucket_id=_PREFIX,
        max_size_bytes=200_000_000,
    )
    assert outcome.success is False
    assert "403" in outcome.stderr
    assert outcome.failure_reason == "os_error"
