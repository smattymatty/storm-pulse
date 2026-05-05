"""Tests for stormpulse.garage.clear_bucket.run_clear_bucket.

Drives the handler with a fake S3 client. Covers the five branches the
spec calls out:

- auth_failed         (HeadBucket raises S3AuthError)
- os_error            (List or Delete raises non-auth S3Error)
- empty bucket        (List returns no objects)
- partial_failure     (DeleteObjects returns errors[] non-empty)
- success             (clean delete of N objects across pagination)

Plus integration with agent dispatch via make_clear_bucket_handler.
"""

from __future__ import annotations

from typing import Any

import pytest

from stormpulse.garage.clear_bucket import (
    make_clear_bucket_handler,
    run_clear_bucket,
)
from stormpulse.garage.s3 import (
    DeleteResult,
    GarageS3Client,
    ListResult,
    S3AuthError,
    S3Error,
    S3ErrorEntry,
    S3ObjectEntry,
)


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


class _FakeS3Client:
    """Pretends to be GarageS3Client. Drives the handler under test."""

    def __init__(
        self,
        *,
        head_raises: Exception | None = None,
        pages: list[ListResult] | None = None,
        list_raises: Exception | None = None,
        delete_results: list[DeleteResult] | None = None,
        delete_raises: Exception | None = None,
    ) -> None:
        self._head_raises = head_raises
        self._pages = pages or [
            ListResult(contents=[], is_truncated=False, next_continuation_token=None, key_count=0),
        ]
        self._page_index = 0
        self._list_raises = list_raises
        self._delete_results = delete_results or []
        self._delete_index = 0
        self._delete_raises = delete_raises
        self.delete_calls: list[list[str]] = []

    def head_bucket(self, bucket: str) -> None:
        if self._head_raises is not None:
            raise self._head_raises

    def list_objects_v2(
        self, bucket: str, continuation_token: str | None = None, max_keys: int = 1000,
    ) -> ListResult:
        if self._list_raises is not None:
            raise self._list_raises
        page = self._pages[self._page_index]
        self._page_index += 1
        return page

    def delete_objects(self, bucket: str, keys: list[str]) -> DeleteResult:
        self.delete_calls.append(list(keys))
        if self._delete_raises is not None:
            raise self._delete_raises
        if self._delete_index < len(self._delete_results):
            result = self._delete_results[self._delete_index]
            self._delete_index += 1
            return result
        return DeleteResult(deleted=list(keys), errors=[])


class _ProgressRecorder:
    """Captures progress callback invocations for assertion."""

    def __init__(self) -> None:
        self.events: list[tuple[str, int, int | None, str]] = []

    async def __call__(
        self, stage: str, current: int, total: int | None, message: str,
    ) -> None:
        self.events.append((stage, current, total, message))


def _make_page(keys: list[str], is_truncated: bool, token: str | None = None) -> ListResult:
    return ListResult(
        contents=[S3ObjectEntry(key=k, size=1) for k in keys],
        is_truncated=is_truncated,
        next_continuation_token=token,
        key_count=len(keys),
    )


# ---------------------------------------------------------------------------
# auth_failed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_failure_returns_auth_failed_outcome() -> None:
    client = _FakeS3Client(head_raises=S3AuthError("403 Forbidden", status=403))
    progress = _ProgressRecorder()

    outcome = await run_clear_bucket(progress, client, "test-bucket")  # type: ignore[arg-type]

    assert outcome.success is False
    assert outcome.failure_reason == "auth_failed"
    assert outcome.extras["deleted_count"] == 0
    assert outcome.extras["failed_count"] == 0
    assert "Admin secret" in outcome.extras["error"]
    # No deletes attempted
    assert client.delete_calls == []
    # First progress emitted is the credential pre-flight
    assert progress.events[0][0] == "starting"


# ---------------------------------------------------------------------------
# os_error from list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_failure_returns_os_error_outcome() -> None:
    client = _FakeS3Client(list_raises=S3Error("500 ServerError", status=500))
    progress = _ProgressRecorder()

    outcome = await run_clear_bucket(progress, client, "test-bucket")  # type: ignore[arg-type]

    assert outcome.success is False
    assert outcome.failure_reason == "os_error"
    assert outcome.extras["deleted_count"] == 0
    assert "500 ServerError" in outcome.extras["error"]


# ---------------------------------------------------------------------------
# empty bucket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_bucket_succeeds_with_zero_counts() -> None:
    client = _FakeS3Client()  # default: empty page
    progress = _ProgressRecorder()

    outcome = await run_clear_bucket(progress, client, "empty-bucket")  # type: ignore[arg-type]

    assert outcome.success is True
    assert outcome.extras["deleted_count"] == 0
    assert outcome.extras["failed_count"] == 0
    assert outcome.extras["errors"] == []
    assert client.delete_calls == []
    assert "duration_seconds" in outcome.extras


# ---------------------------------------------------------------------------
# partial_failure (the bug class from the Django side)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_delete_failure_marks_overall_failure() -> None:
    """P1 contract: per-object errors -> success=false, partial_failure."""
    client = _FakeS3Client(
        pages=[_make_page(["a", "b", "c"], is_truncated=False)],
        delete_results=[
            DeleteResult(
                deleted=["a"],
                errors=[
                    S3ErrorEntry(key="b", code="AccessDenied", message="denied"),
                    S3ErrorEntry(key="c", code="AccessDenied", message="denied"),
                ],
            ),
        ],
    )
    progress = _ProgressRecorder()

    outcome = await run_clear_bucket(progress, client, "test-bucket")  # type: ignore[arg-type]

    assert outcome.success is False
    assert outcome.failure_reason == "partial_failure"
    assert outcome.extras["deleted_count"] == 1
    assert outcome.extras["failed_count"] == 2
    assert len(outcome.extras["errors"]) == 2
    assert outcome.extras["errors"][0] == {"Key": "b", "Code": "AccessDenied", "Message": "denied"}
    assert "could not be deleted" in outcome.extras["error"]


@pytest.mark.asyncio
async def test_errors_are_truncated_to_first_ten() -> None:
    """Wire payload stays small even when many objects fail."""
    keys = [f"k{i}" for i in range(15)]
    client = _FakeS3Client(
        pages=[_make_page(keys, is_truncated=False)],
        delete_results=[
            DeleteResult(
                deleted=[],
                errors=[
                    S3ErrorEntry(key=k, code="AccessDenied", message="x") for k in keys
                ],
            ),
        ],
    )
    progress = _ProgressRecorder()

    outcome = await run_clear_bucket(progress, client, "test-bucket")  # type: ignore[arg-type]

    assert outcome.failure_reason == "partial_failure"
    assert outcome.extras["failed_count"] == 15
    assert len(outcome.extras["errors"]) == 10  # truncated


# ---------------------------------------------------------------------------
# success across pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_success_across_two_pages() -> None:
    page1_keys = [f"k{i}" for i in range(50)]
    page2_keys = [f"k{i}" for i in range(50, 80)]
    client = _FakeS3Client(
        pages=[
            _make_page(page1_keys, is_truncated=True, token="next-token"),
            _make_page(page2_keys, is_truncated=False),
        ],
    )
    progress = _ProgressRecorder()

    outcome = await run_clear_bucket(progress, client, "test-bucket")  # type: ignore[arg-type]

    assert outcome.success is True
    assert outcome.extras["deleted_count"] == 80
    assert outcome.extras["failed_count"] == 0
    # All 80 keys delivered to delete_objects (one batch, since 80 < 1000)
    assert sum(len(c) for c in client.delete_calls) == 80
    # Progress events: starting (creds) + starting (listing) + running (one batch) + finalizing
    stages = [e[0] for e in progress.events]
    assert stages.count("starting") >= 2
    assert "running" in stages
    assert stages[-1] == "finalizing"


@pytest.mark.asyncio
async def test_progress_running_reports_accurate_total() -> None:
    keys = [f"k{i}" for i in range(2500)]  # spans 3 batches of 1000
    client = _FakeS3Client(pages=[_make_page(keys, is_truncated=False)])
    progress = _ProgressRecorder()

    outcome = await run_clear_bucket(progress, client, "test-bucket")  # type: ignore[arg-type]

    assert outcome.success is True
    assert outcome.extras["deleted_count"] == 2500
    # 3 delete batches issued
    assert len(client.delete_calls) == 3
    assert [len(c) for c in client.delete_calls] == [1000, 1000, 500]
    # Running progress events monotonically increasing, all with total=2500
    running_events = [e for e in progress.events if e[0] == "running"]
    assert len(running_events) == 3
    assert running_events[0][2] == 2500  # total field
    assert [e[1] for e in running_events] == [1000, 2000, 2500]  # current values


# ---------------------------------------------------------------------------
# make_clear_bucket_handler
# ---------------------------------------------------------------------------


def test_handler_factory_returns_none_for_missing_params() -> None:
    handler = make_clear_bucket_handler({"bucket_name": "x"})
    assert handler is None


def test_handler_factory_returns_none_for_bad_endpoint() -> None:
    handler = make_clear_bucket_handler({
        "bucket_name": "x",
        "s3_endpoint": "not-a-url",
        "region": "garage",
        "access_key_id": "GK1",
        "secret_access_key": "secret",
    })
    assert handler is None


def test_handler_factory_returns_handler_with_valid_params() -> None:
    handler = make_clear_bucket_handler({
        "bucket_name": "x",
        "s3_endpoint": "http://localhost:3900",
        "region": "garage",
        "access_key_id": "GK1",
        "secret_access_key": "secret",
    })
    assert handler is not None
    assert callable(handler)
