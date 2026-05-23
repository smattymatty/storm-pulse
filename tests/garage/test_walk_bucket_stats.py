"""Tests for stormpulse.garage.walk_bucket_stats.run_walk_bucket_stats.

Drives the handler with a fake S3 client. Covers:

- auth_failed   (HeadBucket raises S3AuthError)
- os_error      (List raises non-auth S3Error)
- empty prefix  (List returns no objects)
- exhausted     (single page, list contents counted + summed)
- paginated     (multi-page walk via continuation token)
- truncated     (max_objects cap hit mid-walk; truncated=True)
- prefix passed (the prefix arg reaches the S3 client)

Plus the factory branch via make_walk_bucket_stats_handler.
"""

from __future__ import annotations

import pytest

from stormpulse.garage.s3 import (
    GarageS3Client,
    ListResult,
    S3AuthError,
    S3Error,
    S3ObjectEntry,
)
from stormpulse.garage.walk_bucket_stats import (
    make_walk_bucket_stats_handler,
    run_walk_bucket_stats,
)


class _FakeS3Client:
    """Pretends to be GarageS3Client. Drives the handler under test."""

    def __init__(
        self,
        *,
        head_raises: Exception | None = None,
        pages: list[ListResult] | None = None,
        list_raises: Exception | None = None,
    ) -> None:
        self._head_raises = head_raises
        self._pages = pages or [
            ListResult(contents=[], is_truncated=False, next_continuation_token=None, key_count=0),
        ]
        self._page_index = 0
        self._list_raises = list_raises
        self.list_calls: list[dict[str, object]] = []

    def head_bucket(self, bucket: str) -> None:
        if self._head_raises is not None:
            raise self._head_raises

    def list_objects_v2(
        self,
        bucket: str,
        continuation_token: str | None = None,
        max_keys: int = 1000,
        prefix: str | None = None,
    ) -> ListResult:
        self.list_calls.append({
            "bucket": bucket,
            "continuation_token": continuation_token,
            "max_keys": max_keys,
            "prefix": prefix,
        })
        if self._list_raises is not None:
            raise self._list_raises
        page = self._pages[self._page_index]
        self._page_index += 1
        return page


async def _noop_progress(*args: object, **kwargs: object) -> None:
    pass


# ---------------------------------------------------------------------------
# Branch coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_failed_short_circuits_before_list() -> None:
    client = _FakeS3Client(head_raises=S3AuthError("forbidden", status=403))
    outcome = await run_walk_bucket_stats(
        _noop_progress, client, "vault", "", 100_000,  # type: ignore[arg-type]
    )
    assert outcome.success is False
    assert outcome.failure_reason == "auth_failed"
    assert outcome.extras["count"] == 0
    assert outcome.extras["bytes"] == 0
    # Did not proceed to list - fake records zero list calls.
    assert client.list_calls == []


@pytest.mark.asyncio
async def test_os_error_during_list_returns_partial_counts() -> None:
    client = _FakeS3Client(list_raises=S3Error("server down", status=500))
    outcome = await run_walk_bucket_stats(
        _noop_progress, client, "vault", "", 100_000,  # type: ignore[arg-type]
    )
    assert outcome.success is False
    assert outcome.failure_reason == "os_error"
    assert outcome.extras["count"] == 0
    assert outcome.extras["bytes"] == 0
    assert outcome.extras["truncated"] is False
    assert "server down" in outcome.extras["error"]


@pytest.mark.asyncio
async def test_empty_listing_returns_zero_counts_and_success() -> None:
    client = _FakeS3Client()  # default page is empty
    outcome = await run_walk_bucket_stats(
        _noop_progress, client, "vault", "", 100_000,  # type: ignore[arg-type]
    )
    assert outcome.success is True
    assert outcome.extras["count"] == 0
    assert outcome.extras["bytes"] == 0
    assert outcome.extras["truncated"] is False


@pytest.mark.asyncio
async def test_single_page_walk_sums_count_and_bytes() -> None:
    page = ListResult(
        contents=[
            S3ObjectEntry(key="a.jpg", size=100),
            S3ObjectEntry(key="b.jpg", size=250),
            S3ObjectEntry(key="c.jpg", size=400),
        ],
        is_truncated=False,
        next_continuation_token=None,
        key_count=3,
    )
    client = _FakeS3Client(pages=[page])
    outcome = await run_walk_bucket_stats(
        _noop_progress, client, "vault", "", 100_000,  # type: ignore[arg-type]
    )
    assert outcome.success is True
    assert outcome.extras["count"] == 3
    assert outcome.extras["bytes"] == 750
    assert outcome.extras["truncated"] is False


@pytest.mark.asyncio
async def test_paginated_walk_follows_continuation_token() -> None:
    pages = [
        ListResult(
            contents=[S3ObjectEntry(key="a", size=10), S3ObjectEntry(key="b", size=20)],
            is_truncated=True,
            next_continuation_token="cursor-1",
            key_count=2,
        ),
        ListResult(
            contents=[S3ObjectEntry(key="c", size=30)],
            is_truncated=False,
            next_continuation_token=None,
            key_count=1,
        ),
    ]
    client = _FakeS3Client(pages=pages)
    outcome = await run_walk_bucket_stats(
        _noop_progress, client, "vault", "", 100_000,  # type: ignore[arg-type]
    )
    assert outcome.success is True
    assert outcome.extras["count"] == 3
    assert outcome.extras["bytes"] == 60
    assert outcome.extras["truncated"] is False
    # Two list calls; second one carried the continuation token.
    assert len(client.list_calls) == 2
    assert client.list_calls[0]["continuation_token"] is None
    assert client.list_calls[1]["continuation_token"] == "cursor-1"


@pytest.mark.asyncio
async def test_max_objects_cap_returns_truncated_true_with_partial_counts() -> None:
    # max_objects=2 - should stop after the 2nd object even though the
    # page has 3, and even though is_truncated would have continued.
    page = ListResult(
        contents=[
            S3ObjectEntry(key="a", size=100),
            S3ObjectEntry(key="b", size=200),
            S3ObjectEntry(key="c", size=400),
        ],
        is_truncated=True,
        next_continuation_token="never-used",
        key_count=3,
    )
    client = _FakeS3Client(pages=[page])
    outcome = await run_walk_bucket_stats(
        _noop_progress, client, "vault", "", 2,  # type: ignore[arg-type]
    )
    assert outcome.success is True
    assert outcome.extras["count"] == 2
    # Bytes match the two counted objects only.
    assert outcome.extras["bytes"] == 300
    assert outcome.extras["truncated"] is True
    # We did NOT request a second page (cap hit mid-first-page).
    assert len(client.list_calls) == 1


@pytest.mark.asyncio
async def test_prefix_is_forwarded_to_list_call() -> None:
    page = ListResult(
        contents=[S3ObjectEntry(key="photos/a", size=1)],
        is_truncated=False,
        next_continuation_token=None,
        key_count=1,
    )
    client = _FakeS3Client(pages=[page])
    await run_walk_bucket_stats(
        _noop_progress, client, "vault", "photos/", 100_000,  # type: ignore[arg-type]
    )
    assert client.list_calls[0]["prefix"] == "photos/"


@pytest.mark.asyncio
async def test_empty_prefix_passes_none_to_client() -> None:
    page = ListResult(
        contents=[],
        is_truncated=False,
        next_continuation_token=None,
        key_count=0,
    )
    client = _FakeS3Client(pages=[page])
    await run_walk_bucket_stats(
        _noop_progress, client, "vault", "", 100_000,  # type: ignore[arg-type]
    )
    # Empty prefix → None on the client call (matches the
    # ``prefix or None`` line in the handler; the S3 query string omits
    # ``prefix`` when None, which is the desired "list everything" form).
    assert client.list_calls[0]["prefix"] is None


# ---------------------------------------------------------------------------
# Factory branch
# ---------------------------------------------------------------------------


def test_factory_returns_none_when_required_param_missing() -> None:
    handler = make_walk_bucket_stats_handler({
        "bucket_name": "vault",
        # missing s3_endpoint
        "region": "garage",
        "access_key_id": "GKADMIN",
        "secret_access_key": "shh",
    })
    assert handler is None


def test_factory_constructs_handler_when_params_complete() -> None:
    handler = make_walk_bucket_stats_handler({
        "bucket_name": "vault",
        "s3_endpoint": "https://s3.local.test",
        "region": "garage",
        "access_key_id": "GKADMIN",
        "secret_access_key": "shh",
        "prefix": "photos/",
        "max_objects": "100",
    })
    assert handler is not None


def test_factory_invalid_max_objects_falls_back_to_default() -> None:
    # max_objects="not-a-number" - handler shouldn't crash; falls back
    # to the default cap. We can't easily test the value without running
    # the handler, but at least constructing it must succeed.
    handler = make_walk_bucket_stats_handler({
        "bucket_name": "vault",
        "s3_endpoint": "https://s3.local.test",
        "region": "garage",
        "access_key_id": "GKADMIN",
        "secret_access_key": "shh",
        "max_objects": "not-a-number",
    })
    assert handler is not None
