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

from pathlib import Path
from typing import Any

import pytest

from stormpulse.commands.jobs import JobOutcome
from stormpulse.garage.clear_bucket import (
    make_clear_bucket_handler,
    run_clear_bucket,
    run_clear_bucket_credential_less,
)
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.s3 import (
    DeleteResult,
    ListResult,
    S3AuthError,
    S3Error,
    S3ErrorEntry,
    S3ObjectEntry,
)

_PREFIX = "f1dc32249aa1d80a"  # Storm's 16-char garage_bucket_id

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
            ListResult(
                contents=[],
                is_truncated=False,
                next_continuation_token=None,
                key_count=0,
            ),
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
        self,
        bucket: str,
        continuation_token: str | None = None,
        max_keys: int = 1000,
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
        self,
        stage: str,
        current: int,
        total: int | None,
        message: str,
    ) -> None:
        self.events.append((stage, current, total, message))


def _make_page(
    keys: list[str], is_truncated: bool, token: str | None = None
) -> ListResult:
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
    assert outcome.extras["errors"][0] == {
        "Key": "b",
        "Code": "AccessDenied",
        "Message": "denied",
    }
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


def _make_config(*, admin: bool = True) -> GarageConfig:
    return GarageConfig(
        enabled=True,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=Path("/opt/garage/garage.toml"),
        admin_url="http://127.0.0.1:3903" if admin else "",
        admin_token="tok" if admin else "",
    )


def test_handler_factory_returns_none_for_missing_params() -> None:
    handler = make_clear_bucket_handler(_make_config(), {"bucket_name": "x"})
    assert handler is None


def test_handler_factory_returns_none_for_bad_endpoint() -> None:
    handler = make_clear_bucket_handler(
        _make_config(),
        {
            "bucket_name": "x",
            "s3_endpoint": "not-a-url",
            "region": "garage",
            "access_key_id": "GK1",
            "secret_access_key": "secret",
        },
    )
    assert handler is None


def test_handler_factory_returns_handler_with_valid_params() -> None:
    handler = make_clear_bucket_handler(
        _make_config(),
        {
            "bucket_name": "x",
            "s3_endpoint": "http://localhost:3900",
            "region": "garage",
            "access_key_id": "GK1",
            "secret_access_key": "secret",
        },
    )
    assert handler is not None
    assert callable(handler)


def test_handler_factory_rejects_half_a_credential_pair() -> None:
    handler = make_clear_bucket_handler(
        _make_config(),
        {
            "bucket_name": "x",
            "s3_endpoint": "http://localhost:3900",
            "region": "garage",
            "access_key_id": "GK1",
        },
    )
    assert handler is None


def test_handler_factory_credential_less_requires_bucket_id() -> None:
    handler = make_clear_bucket_handler(
        _make_config(),
        {
            "s3_endpoint": "http://localhost:3900",
            "region": "garage",
        },
    )
    assert handler is None


def test_handler_factory_returns_credential_less_handler() -> None:
    handler = make_clear_bucket_handler(
        _make_config(),
        {
            "bucket_id": _PREFIX,
            "s3_endpoint": "http://localhost:3900",
            "region": "garage",
        },
    )
    assert handler is not None
    assert callable(handler)


# ---------------------------------------------------------------------------
# credential-less purge clear (ADR BUCKETS-010)
# ---------------------------------------------------------------------------


class _FakeAdmin:
    """Records admin_api calls and returns configurable canned results."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.create_key_result: tuple[dict[str, Any] | None, str] = (
            {"accessKeyId": "GKPURGE", "secretAccessKey": "purge-secret"},
            "",
        )
        self.allow_result: tuple[bool, str] = (True, "")
        self.alias_result: tuple[bool, str] = (True, "")
        self.delete_key_result: tuple[bool, str] = (True, "")

    def create_key(
        self, *, admin_url: str, admin_token: str, name: str,
    ) -> tuple[dict[str, Any] | None, str]:
        self.calls.append(("create_key", {"name": name}))
        return self.create_key_result

    def allow_bucket_key(
        self, *, admin_url: str, admin_token: str, bucket_ref: str,
        access_key_id: str, read: bool, write: bool, owner: bool = False,
    ) -> tuple[bool, str]:
        self.calls.append(
            (
                "allow_bucket_key",
                {
                    "bucket_ref": bucket_ref,
                    "access_key_id": access_key_id,
                    "read": read,
                    "write": write,
                    "owner": owner,
                },
            )
        )
        return self.allow_result

    def add_bucket_alias_local(
        self, *, admin_url: str, admin_token: str, bucket_ref: str,
        access_key_id: str, local_alias: str,
    ) -> tuple[bool, str]:
        self.calls.append(
            (
                "add_bucket_alias_local",
                {
                    "bucket_ref": bucket_ref,
                    "access_key_id": access_key_id,
                    "local_alias": local_alias,
                },
            )
        )
        return self.alias_result

    def delete_key(
        self, *, admin_url: str, admin_token: str, access_key_id: str,
    ) -> tuple[bool, str]:
        self.calls.append(("delete_key", {"access_key_id": access_key_id}))
        return self.delete_key_result

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _patch_admin(monkeypatch: pytest.MonkeyPatch, fake: _FakeAdmin) -> None:
    for fn in (
        "create_key",
        "allow_bucket_key",
        "add_bucket_alias_local",
        "delete_key",
    ):
        monkeypatch.setattr(
            f"stormpulse.garage.clear_bucket.admin_api.{fn}",
            getattr(fake, fn),
        )


def _patch_s3_client(
    monkeypatch: pytest.MonkeyPatch, client: _FakeS3Client,
) -> dict[str, str]:
    """Substitute the GarageS3Client constructor; record the creds it got."""
    seen: dict[str, str] = {}

    def fake_ctor(
        *, endpoint: str, region: str, access_key: str, secret_key: str,
    ) -> _FakeS3Client:
        seen.update(
            endpoint=endpoint, region=region,
            access_key=access_key, secret_key=secret_key,
        )
        return client

    monkeypatch.setattr(
        "stormpulse.garage.clear_bucket.GarageS3Client", fake_ctor,
    )
    return seen


async def _run_credential_less(
    fake_admin: _FakeAdmin,
    *,
    admin: bool = True,
) -> tuple[JobOutcome, _ProgressRecorder]:
    progress = _ProgressRecorder()
    outcome = await run_clear_bucket_credential_less(
        progress=progress,
        garage_config=_make_config(admin=admin),
        bucket_id=_PREFIX,
        endpoint="http://localhost:3900",
        region="garage",
    )
    return outcome, progress


@pytest.mark.asyncio
async def test_credential_less_mints_grants_aliases_clears_destroys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAdmin()
    _patch_admin(monkeypatch, fake)
    client = _FakeS3Client(pages=[_make_page(["a", "b"], is_truncated=False)])
    seen = _patch_s3_client(monkeypatch, client)

    outcome, _ = await _run_credential_less(fake)

    assert outcome.success is True
    assert outcome.extras["deleted_count"] == 2
    assert outcome.extras["credential_less"] is True
    assert outcome.extras["purge_key_id"] == "GKPURGE"
    assert outcome.extras["manual_cleanup_required"] == []
    # Full sequence, key destroyed last.
    assert fake.names() == [
        "create_key", "allow_bucket_key", "add_bucket_alias_local", "delete_key",
    ]
    # The key is granted on the bucket ID, the clear runs via the
    # key-scoped alias (S3 addresses by name, never id), and the alias
    # carries the agent-side purge name, never a customer-chosen one.
    assert fake.calls[1][1]["bucket_ref"] == _PREFIX
    alias = fake.calls[2][1]["local_alias"]
    assert alias == f"{_PREFIX[:8]}-purge"
    assert seen["access_key"] == "GKPURGE"
    assert seen["secret_key"] == "purge-secret"


@pytest.mark.asyncio
async def test_credential_less_secret_never_in_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAdmin()
    _patch_admin(monkeypatch, fake)
    client = _FakeS3Client(pages=[_make_page(["a"], is_truncated=False)])
    _patch_s3_client(monkeypatch, client)

    outcome, _ = await _run_credential_less(fake)

    wire = repr(outcome)
    assert "purge-secret" not in wire


@pytest.mark.asyncio
async def test_credential_less_unconfigured_admin_fails_loud() -> None:
    outcome, _ = await _run_credential_less(_FakeAdmin(), admin=False)

    assert outcome.success is False
    assert outcome.failure_reason == "admin_api_unconfigured"
    assert "admin_token_file" in outcome.stderr


@pytest.mark.asyncio
async def test_credential_less_mint_failure_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAdmin()
    fake.create_key_result = (None, "503 admin API down")
    _patch_admin(monkeypatch, fake)

    outcome, _ = await _run_credential_less(fake)

    assert outcome.success is False
    assert outcome.failure_reason == "purge_key_mint_failed"
    assert "503 admin API down" in outcome.stderr
    # Nothing was minted, so nothing to destroy.
    assert "delete_key" not in fake.names()


@pytest.mark.asyncio
async def test_credential_less_grant_failure_still_destroys_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAdmin()
    fake.allow_result = (False, "no such bucket")
    _patch_admin(monkeypatch, fake)

    outcome, _ = await _run_credential_less(fake)

    assert outcome.success is False
    assert outcome.failure_reason == "purge_key_grant_failed"
    assert fake.names()[-1] == "delete_key"


@pytest.mark.asyncio
async def test_credential_less_alias_failure_still_destroys_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAdmin()
    fake.alias_result = (False, "alias rejected")
    _patch_admin(monkeypatch, fake)

    outcome, _ = await _run_credential_less(fake)

    assert outcome.success is False
    assert outcome.failure_reason == "purge_alias_failed"
    assert fake.names()[-1] == "delete_key"


@pytest.mark.asyncio
async def test_credential_less_clear_failure_still_destroys_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAdmin()
    _patch_admin(monkeypatch, fake)
    client = _FakeS3Client(
        head_raises=S3AuthError("403 Forbidden", status=403),
    )
    _patch_s3_client(monkeypatch, client)

    outcome, _ = await _run_credential_less(fake)

    assert outcome.success is False
    assert outcome.failure_reason == "auth_failed"
    assert fake.names()[-1] == "delete_key"
    assert outcome.extras["credential_less"] is True


@pytest.mark.asyncio
async def test_credential_less_leaked_key_is_loud_in_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAdmin()
    fake.delete_key_result = (False, "admin API hiccup")
    _patch_admin(monkeypatch, fake)
    client = _FakeS3Client(pages=[_make_page(["a"], is_truncated=False)])
    _patch_s3_client(monkeypatch, client)

    outcome, _ = await _run_credential_less(fake)

    # The clear itself succeeded; the leaked key rides the result loudly.
    assert outcome.success is True
    assert outcome.extras["manual_cleanup_required"] == [
        {"type": "key", "id": "GKPURGE"},
    ]
