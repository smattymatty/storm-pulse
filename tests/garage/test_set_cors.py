"""Tests for stormpulse.garage.set_cors.

Drives the handler with a fake S3 client. Covers:

- happy path        (PutBucketCors called with the expected CorsRule)
- auth_failed       (S3AuthError -> failure_reason="auth_failed")
- os_error          (other S3Error -> failure_reason="os_error")
- origins decoding  (JSON-string param -> list[str] in the rule)
- factory disposition for missing/malformed params
"""

from __future__ import annotations

import pytest

from stormpulse.garage import set_cors
from stormpulse.garage.s3 import (
    CorsRule,
    GarageS3Client,
    S3AuthError,
    S3Error,
)
from stormpulse.garage.set_cors import (
    ALLOWED_HEADERS,
    ALLOWED_METHODS,
    EXPOSE_HEADERS,
    MAX_AGE_SECONDS,
    make_set_cors_handler,
    run_set_cors,
)


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


class _FakeS3Client:
    """Pretends to be GarageS3Client. Drives the handler under test."""

    def __init__(self, *, put_raises: Exception | None = None) -> None:
        self._put_raises = put_raises
        self.put_calls: list[tuple[str, CorsRule]] = []

    def put_bucket_cors(self, bucket: str, rule: CorsRule) -> None:
        self.put_calls.append((bucket, rule))
        if self._put_raises is not None:
            raise self._put_raises


class _ProgressRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, int, int | None, str]] = []

    async def __call__(
        self, stage: str, current: int, total: int | None, message: str,
    ) -> None:
        self.events.append((stage, current, total, message))


_VALID_PARAMS = {
    "bucket_name": "media",
    "s3_endpoint": "http://localhost:3900",
    "region": "garage",
    "access_key_id": "GK1",
    "secret_access_key": "secret",
    "origins": '["https://stormdevelopments.ca"]',
}


# ---------------------------------------------------------------------------
# run_set_cors — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_calls_put_bucket_cors_with_correct_rule() -> None:
    client = _FakeS3Client()
    progress = _ProgressRecorder()
    origins = ["https://stormdevelopments.ca"]

    outcome = await run_set_cors(progress, client, "media", origins)  # type: ignore[arg-type]

    assert outcome.success is True
    assert outcome.failure_reason is None
    assert len(client.put_calls) == 1
    bucket, rule = client.put_calls[0]
    assert bucket == "media"
    assert rule.allowed_origins == origins
    assert rule.allowed_methods == ALLOWED_METHODS
    assert rule.allowed_headers == ALLOWED_HEADERS
    assert rule.expose_headers == EXPOSE_HEADERS
    assert rule.max_age_seconds == MAX_AGE_SECONDS
    # Outcome echoes the rule back in extras
    assert outcome.extras["origins"] == origins
    assert outcome.extras["allowed_methods"] == ALLOWED_METHODS
    assert outcome.extras["allowed_headers"] == ALLOWED_HEADERS
    assert outcome.extras["expose_headers"] == EXPOSE_HEADERS
    assert outcome.extras["max_age_seconds"] == MAX_AGE_SECONDS
    assert "duration_seconds" in outcome.extras
    # Progress events: starting then finalizing
    stages = [e[0] for e in progress.events]
    assert stages == ["starting", "finalizing"]


@pytest.mark.asyncio
async def test_happy_path_with_multiple_origins() -> None:
    client = _FakeS3Client()
    progress = _ProgressRecorder()
    origins = ["https://stormdevelopments.ca", "https://example.com"]

    outcome = await run_set_cors(progress, client, "media", origins)  # type: ignore[arg-type]

    assert outcome.success is True
    bucket, rule = client.put_calls[0]
    assert rule.allowed_origins == origins
    assert outcome.stdout == f"Applied CORS rule with {len(origins)} origin(s)"


# ---------------------------------------------------------------------------
# run_set_cors — failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_failure_returns_auth_failed_outcome() -> None:
    client = _FakeS3Client(put_raises=S3AuthError("403 Forbidden", status=403))
    progress = _ProgressRecorder()

    outcome = await run_set_cors(progress, client, "media", ["https://x"])  # type: ignore[arg-type]

    assert outcome.success is False
    assert outcome.failure_reason == "auth_failed"
    assert "Admin secret" in outcome.extras["error"]
    assert "duration_seconds" in outcome.extras


@pytest.mark.asyncio
async def test_other_s3_error_returns_os_error_outcome() -> None:
    client = _FakeS3Client(put_raises=S3Error("500 ServerError", status=500))
    progress = _ProgressRecorder()

    outcome = await run_set_cors(progress, client, "media", ["https://x"])  # type: ignore[arg-type]

    assert outcome.success is False
    assert outcome.failure_reason == "os_error"
    assert "500 ServerError" in outcome.extras["error"]


@pytest.mark.asyncio
async def test_no_such_bucket_surfaces_in_extras_error() -> None:
    """Ops triage: the S3 error code rides through extras.error."""
    client = _FakeS3Client(
        put_raises=S3Error(
            "PUT /missing -> HTTP 404: NoSuchBucket: bucket gone",
            status=404, code="NoSuchBucket",
        ),
    )
    progress = _ProgressRecorder()

    outcome = await run_set_cors(progress, client, "missing", ["https://x"])  # type: ignore[arg-type]

    assert outcome.failure_reason == "os_error"
    assert "NoSuchBucket" in outcome.extras["error"]


# ---------------------------------------------------------------------------
# make_set_cors_handler — origins decoding
# ---------------------------------------------------------------------------


def test_handler_factory_returns_handler_with_valid_params() -> None:
    handler = make_set_cors_handler(_VALID_PARAMS)
    assert handler is not None
    assert callable(handler)


@pytest.mark.asyncio
async def test_origins_param_decoded_from_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """The factory decodes the JSON-string and the decoded list reaches run_set_cors."""
    captured: dict[str, list[str]] = {}

    async def fake_run(progress, client, bucket, origins):  # type: ignore[no-untyped-def]
        captured["origins"] = origins
        # Return the real success shape so the handler doesn't blow up
        from stormpulse.commands.jobs import JobOutcome
        return JobOutcome(success=True, exit_code=0)

    monkeypatch.setattr(set_cors, "run_set_cors", fake_run)

    params = dict(_VALID_PARAMS)
    params["origins"] = '["https://a.example", "https://b.example"]'
    handler = make_set_cors_handler(params)
    assert handler is not None

    progress = _ProgressRecorder()
    await handler(progress)

    assert captured["origins"] == ["https://a.example", "https://b.example"]


def test_origins_param_rejects_malformed_json() -> None:
    """Decode failures, wrong types, and empty/non-string entries -> factory returns None."""
    bad_origins = [
        "not-json",
        "{}",
        "[]",  # empty list rejected
        "[1, 2]",  # not strings
        '["", "valid"]',  # empty-string entry
        "null",
    ]
    for bad in bad_origins:
        params = dict(_VALID_PARAMS)
        params["origins"] = bad
        handler = make_set_cors_handler(params)
        assert handler is None, f"expected None for origins={bad!r}"


# ---------------------------------------------------------------------------
# make_set_cors_handler — missing params
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing",
    ["bucket_name", "s3_endpoint", "region", "access_key_id", "secret_access_key", "origins"],
)
def test_handler_factory_returns_none_for_missing_param(missing: str) -> None:
    params = dict(_VALID_PARAMS)
    del params[missing]
    handler = make_set_cors_handler(params)
    assert handler is None


def test_handler_factory_returns_none_for_bad_endpoint() -> None:
    params = dict(_VALID_PARAMS)
    params["s3_endpoint"] = "not-a-url"
    handler = make_set_cors_handler(params)
    assert handler is None
