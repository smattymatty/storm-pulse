"""Tests for the incomplete-upload reclaim: the admin-API client + the handler.

The wire tier proves this works against a real Garage. These prove the request
shape, the age-bound plumbing, and every way the handler refuses to run, none
of which needs a container.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from stormpulse.garage import admin_api
from stormpulse.garage.jobs.cleanup_uploads import (
    make_cleanup_uploads_handler,
    run_cleanup_uploads,
)

_ADMIN = {"admin_url": "http://127.0.0.1:3903", "admin_token": "tok"}
_PREFIX = "8742c023e7e97dc8"  # Storm's 16-char garage_bucket_id
_FULL_ID = _PREFIX + "efbd1b9371e8148cea3ad02f12dc5bd75dc91c92e0faaa56"  # 64 chars


class _ProgressRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, int, int | None, str]] = []

    async def __call__(
        self, stage: str, current: int, total: int | None, message: str,
        *,
        transfer: object | None = None,
        bytes_freed: object | None = None,
    ) -> None:
        self.events.append((stage, current, total, message))


def _install_fake_admin(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cleanup_status: int = 200,
    cleanup_body: bytes = b'{"uploadsDeleted": 3}',
) -> list[dict[str, Any]]:
    """Fake HTTPConnection answering GetBucketInfo then CleanupIncompleteUploads."""
    requests: list[dict[str, Any]] = []
    resolved = json.dumps({"id": _FULL_ID}).encode()

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
            requests.append(
                {"method": method, "path": path, "body": body, "headers": headers}
            )
            self._path = path

        def getresponse(self) -> _Resp:
            if "GetBucketInfo" in self._path:
                return _Resp(200, resolved)
            return _Resp(cleanup_status, cleanup_body)

        def close(self) -> None:
            pass

    monkeypatch.setattr("http.client.HTTPConnection", _Conn)
    return requests


# ---------------------------------------------------------------------------
# admin_api.cleanup_incomplete_uploads
# ---------------------------------------------------------------------------


def test_resolves_the_prefix_then_posts_the_full_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garage rejects the 16-char prefix on this endpoint, as on every mutator."""
    requests = _install_fake_admin(monkeypatch)

    deleted, err = admin_api.cleanup_incomplete_uploads(
        **_ADMIN, bucket_ref=_PREFIX, older_than_secs=86_400
    )

    assert (deleted, err) == (3, "")
    assert requests[0]["method"] == "GET"
    assert f"search={_PREFIX}" in requests[0]["path"]
    assert requests[1]["method"] == "POST"
    assert requests[1]["path"] == "/v2/CleanupIncompleteUploads"
    assert json.loads(requests[1]["body"]) == {
        "bucketId": _FULL_ID,
        "olderThanSecs": 86_400,
    }


def test_the_age_bound_rides_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cutoff is the safety; it must not be rounded, defaulted, or dropped."""
    requests = _install_fake_admin(monkeypatch)

    admin_api.cleanup_incomplete_uploads(
        **_ADMIN, bucket_ref=_FULL_ID, older_than_secs=7
    )

    assert json.loads(requests[-1]["body"])["olderThanSecs"] == 7


def test_a_non_2xx_is_an_error_not_a_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed cleanup must not read as "nothing needed cleaning"."""
    _install_fake_admin(monkeypatch, cleanup_status=500, cleanup_body=b"boom")

    deleted, err = admin_api.cleanup_incomplete_uploads(
        **_ADMIN, bucket_ref=_FULL_ID, older_than_secs=0
    )

    assert deleted is None
    assert err


def test_a_body_without_the_count_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 whose shape changed is a version break, not a successful zero."""
    _install_fake_admin(monkeypatch, cleanup_body=b'{"somethingElse": 1}')

    deleted, err = admin_api.cleanup_incomplete_uploads(
        **_ADMIN, bucket_ref=_FULL_ID, older_than_secs=0
    )

    assert deleted is None
    assert "unexpected body" in err


# ---------------------------------------------------------------------------
# run_cleanup_uploads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_reports_the_number_aborted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_admin(monkeypatch)
    progress = _ProgressRecorder()

    outcome = await run_cleanup_uploads(
        progress, **_ADMIN, bucket_id=_PREFIX, older_than_secs=3600
    )

    assert outcome.success
    assert outcome.extras["uploads_aborted"] == 3
    assert outcome.extras["older_than_secs"] == 3600
    assert "3 incomplete upload(s)" in outcome.stdout


@pytest.mark.asyncio
async def test_run_surfaces_the_failure_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_admin(monkeypatch, cleanup_status=503, cleanup_body=b"down")
    progress = _ProgressRecorder()

    outcome = await run_cleanup_uploads(
        progress, **_ADMIN, bucket_id=_FULL_ID, older_than_secs=0
    )

    assert not outcome.success
    assert outcome.failure_reason == "os_error"
    assert "CleanupIncompleteUploads failed" in outcome.stderr


# ---------------------------------------------------------------------------
# Handler admission: every way it refuses to run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"bucket_id": _PREFIX},                    # no age
        {"older_than_secs": "60"},                 # no bucket
        {"bucket_id": _PREFIX, "older_than_secs": ""},
    ],
)
def test_missing_params_refuse_the_handler(params: dict[str, str]) -> None:
    assert make_cleanup_uploads_handler(params, **_ADMIN) is None


@pytest.mark.parametrize("age", ["soon", "1.5", "-1", ""])
def test_a_non_integer_or_negative_age_refuses_the_handler(age: str) -> None:
    """A malformed age must not fall back to a default.

    A default here would abort uploads the caller never authorised aborting,
    which is the exact failure the age bound exists to prevent.
    """
    handler = make_cleanup_uploads_handler(
        {"bucket_id": _PREFIX, "older_than_secs": age}, **_ADMIN
    )
    assert handler is None


def test_zero_age_is_allowed() -> None:
    """Zero is a legitimate, deliberate "abort everything in flight".

    Distinct from a missing value: the caller stated it.
    """
    handler = make_cleanup_uploads_handler(
        {"bucket_id": _PREFIX, "older_than_secs": "0"}, **_ADMIN
    )
    assert handler is not None


def test_an_unconfigured_admin_api_refuses_the_handler() -> None:
    """Fails loudly rather than silently leaving the bytes resident."""
    assert (
        make_cleanup_uploads_handler(
            {"bucket_id": _PREFIX, "older_than_secs": "60"},
            admin_url="",
            admin_token="",
        )
        is None
    )
