"""Tests for stormpulse.garage.jobs.clear_bucket.run_clear_bucket.

The clear is a self-converging drain loop: each round lists a page from
the front, deletes it in small batches, and re-lists until the list comes
back empty. The fake client below is therefore STATEFUL - list reflects
what delete removed - because that is the only way to exercise convergence.

Branches covered:

- auth_failed     (ListObjectsV2, the credential proof, raises S3AuthError)
- clear_stalled   (persistent transport error, or a permanently stuck object)
- empty bucket    (List returns no objects)
- success         (clean drain across one or many list rounds)
- transient tolerance (a delete times out but the loop still converges) - the
  regression for the "0B in real time yet 'timed out'" bug
- bytes_freed / object counts reported, counting up

Plus integration with agent dispatch via make_clear_bucket_handler.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stormpulse.commands.jobs import JobOutcome
from stormpulse.garage.jobs.clear_bucket import (
    make_clear_bucket_handler,
    run_clear_bucket,
    run_clear_bucket_credential_less,
)
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.s3 import (
    DeleteResult,
    ListResult,
    MultipartListResult,
    MultipartUpload,
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
    """Stateful in-memory bucket modelling the drain loop's list/delete.

    ``list_objects_v2`` returns the currently-remaining keys (up to
    ``max_keys``); ``delete_objects`` removes them. An empty list is thus
    reached only when every object is genuinely gone, which is exactly the
    convergence signal the loop relies on.

    Failure injection:
    - ``list_raises``: raised on every list call.
    - ``stuck_keys``: keys DeleteObjects always reports as errors and never
      removes (a permission/lock failure that can't be retried away).
    - ``delete_raises_times``: raise a transport S3Error on the first N
      delete calls (a transient timeout).
    - ``delete_frees_before_raise``: when raising, still remove the keys
      from state first, modelling "Garage deleted server-side but the
      response timed out" - the precise shape of the reported bug.
    - ``uploads``: in-flight multipart uploads. Modelled because an empty
      object list is NOT proof the bucket holds nothing: MPU parts are
      resident but invisible to ListObjectsV2.
    - ``uploads_list_raises``: raised on every list-uploads call, so the
      "cannot confirm" branch is reachable.
    """

    def __init__(
        self,
        *,
        objects: dict[str, int] | None = None,
        list_raises: Exception | None = None,
        stuck_keys: set[str] | None = None,
        delete_raises_times: int = 0,
        delete_frees_before_raise: bool = False,
        uploads: dict[str, str] | None = None,
        uploads_list_raises: Exception | None = None,
    ) -> None:
        self._objects = dict(objects or {})
        self._list_raises = list_raises
        self._stuck = set(stuck_keys or ())
        self._delete_raises_times = delete_raises_times
        self._delete_frees_before_raise = delete_frees_before_raise
        self._uploads = dict(uploads or {})
        self._uploads_list_raises = uploads_list_raises
        self.delete_calls: list[list[str]] = []
        self.list_calls = 0
        self.aborted: list[tuple[str, str]] = []

    def list_multipart_uploads(
        self, bucket: str, max_uploads: int = 1000
    ) -> MultipartListResult:
        if self._uploads_list_raises is not None:
            raise self._uploads_list_raises
        return MultipartListResult(
            uploads=[
                MultipartUpload(key=k, upload_id=v)
                for k, v in self._uploads.items()
            ],
            is_truncated=False,
        )

    def abort_multipart_upload(self, bucket: str, key: str, upload_id: str) -> None:
        self.aborted.append((key, upload_id))
        self._uploads.pop(key, None)

    def head_bucket(self, bucket: str) -> None:  # pragma: no cover - unused now
        pass

    def list_objects_v2(
        self,
        bucket: str,
        continuation_token: str | None = None,
        max_keys: int = 1000,
        prefix: str | None = None,
    ) -> ListResult:
        self.list_calls += 1
        if self._list_raises is not None:
            raise self._list_raises
        keys = list(self._objects)[:max_keys]
        return ListResult(
            contents=[S3ObjectEntry(key=k, size=self._objects[k]) for k in keys],
            is_truncated=len(self._objects) > len(keys),
            next_continuation_token=None,
            key_count=len(keys),
        )

    def delete_objects(self, bucket: str, keys: list[str]) -> DeleteResult:
        self.delete_calls.append(list(keys))
        if self._delete_raises_times > 0:
            self._delete_raises_times -= 1
            if self._delete_frees_before_raise:
                for k in keys:
                    if k not in self._stuck:
                        self._objects.pop(k, None)
            raise S3Error("POST /b -> transport error: The read operation timed out")
        deleted: list[str] = []
        errors: list[S3ErrorEntry] = []
        for k in keys:
            if k in self._stuck:
                errors.append(
                    S3ErrorEntry(key=k, code="AccessDenied", message="denied"),
                )
            else:
                self._objects.pop(k, None)
                deleted.append(k)
        return DeleteResult(deleted=deleted, errors=errors)


class _ProgressRecorder:
    """Captures progress callback invocations for assertion."""

    def __init__(self) -> None:
        self.events: list[tuple[str, int, int | None, str, int | None]] = []

    async def __call__(
        self,
        stage: str,
        current: int,
        total: int | None,
        message: str,
        *,
        transfer: object | None = None,
        bytes_freed: int | None = None,
    ) -> None:
        self.events.append((stage, current, total, message, bytes_freed))


# ---------------------------------------------------------------------------
# auth_failed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_failure_returns_auth_failed_outcome() -> None:
    # The list is the credential proof (no separate HeadBucket pre-flight): a
    # wrong key raises S3AuthError from ListObjectsV2, and the ordered catch
    # keeps it classified as auth_failed rather than a stalled-clear give-up.
    client = _FakeS3Client(list_raises=S3AuthError("403 Forbidden", status=403))
    progress = _ProgressRecorder()

    outcome = await run_clear_bucket(progress, client, "test-bucket")  # type: ignore[arg-type]

    assert outcome.success is False
    assert outcome.failure_reason == "auth_failed"
    assert outcome.extras["deleted_count"] == 0
    assert outcome.extras["bytes_freed"] == 0
    assert "Admin secret" in outcome.extras["error"]
    assert client.delete_calls == []
    # The modal leaves "0 objects" the instant the job starts, before the
    # first (failing) list, so the customer never sees a frozen loader.
    assert progress.events[0][0] == "running"


# ---------------------------------------------------------------------------
# clear_stalled - persistent transport error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persistent_list_failure_stalls_after_bounded_rounds() -> None:
    client = _FakeS3Client(list_raises=S3Error("500 ServerError", status=500))
    progress = _ProgressRecorder()

    outcome = await run_clear_bucket(progress, client, "test-bucket")  # type: ignore[arg-type]

    assert outcome.success is False
    assert outcome.failure_reason == "clear_stalled"
    assert outcome.extras["deleted_count"] == 0
    assert "500 ServerError" in outcome.extras["error"]
    # Bounded: it gives up, it does not spin forever.
    assert client.list_calls == 3


# ---------------------------------------------------------------------------
# empty bucket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_bucket_succeeds_with_zero_counts() -> None:
    client = _FakeS3Client()  # no objects
    progress = _ProgressRecorder()

    outcome = await run_clear_bucket(progress, client, "empty-bucket")  # type: ignore[arg-type]

    assert outcome.success is True
    assert outcome.extras["deleted_count"] == 0
    assert outcome.extras["bytes_freed"] == 0
    assert outcome.extras["failed_count"] == 0
    assert outcome.extras["errors"] == []
    assert client.delete_calls == []
    assert "duration_seconds" in outcome.extras


# ---------------------------------------------------------------------------
# success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clean_drain_reports_objects_and_bytes() -> None:
    client = _FakeS3Client(objects={"a": 100, "b": 200, "c": 300})
    progress = _ProgressRecorder()

    outcome = await run_clear_bucket(progress, client, "test-bucket")  # type: ignore[arg-type]

    assert outcome.success is True
    assert outcome.extras["deleted_count"] == 3
    assert outcome.extras["bytes_freed"] == 600
    assert outcome.extras["failed_count"] == 0
    assert outcome.extras["errors"] == []
    # Progress counts UP and carries bytes_freed; the terminal stage is
    # finalizing, and total is always None (a drain has no known total).
    running = [e for e in progress.events if e[0] == "running"]
    assert running[-1][1] == 3  # current (objects)
    assert running[-1][4] == 600  # bytes_freed
    assert all(e[2] is None for e in progress.events)  # no total on the wire
    assert progress.events[-1][0] == "finalizing"


@pytest.mark.asyncio
async def test_drain_across_multiple_list_rounds() -> None:
    # 1500 objects, list pages cap at 1000, so the drain needs two populated
    # list rounds plus the final empty one.
    client = _FakeS3Client(objects={f"k{i}": 1 for i in range(1500)})
    progress = _ProgressRecorder()

    outcome = await run_clear_bucket(progress, client, "test-bucket")  # type: ignore[arg-type]

    assert outcome.success is True
    assert outcome.extras["deleted_count"] == 1500
    assert client.list_calls == 3  # 1000, 500, empty
    # 250-key delete sub-batches: 4 for the first page, 2 for the second.
    assert [len(c) for c in client.delete_calls] == [250, 250, 250, 250, 250, 250]


@pytest.mark.asyncio
async def test_delete_batches_capped_at_250() -> None:
    client = _FakeS3Client(objects={f"k{i}": 1 for i in range(600)})
    progress = _ProgressRecorder()

    outcome = await run_clear_bucket(progress, client, "test-bucket")  # type: ignore[arg-type]

    assert outcome.success is True
    assert [len(c) for c in client.delete_calls] == [250, 250, 100]


# ---------------------------------------------------------------------------
# transient tolerance - the reported-bug regression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_delete_timeout_then_converges() -> None:
    """A delete raises a transport timeout once; the loop retries and succeeds.

    Garage did NOT process this batch, so the keys reappear on the next list
    and get deleted. The old code aborted the whole job here.
    """
    client = _FakeS3Client(objects={"a": 1, "b": 1}, delete_raises_times=1)
    progress = _ProgressRecorder()

    outcome = await run_clear_bucket(progress, client, "test-bucket")  # type: ignore[arg-type]

    assert outcome.success is True
    assert outcome.extras["deleted_count"] == 2


@pytest.mark.asyncio
async def test_timeout_after_server_side_delete_still_converges() -> None:
    """Garage deletes server-side but the response times out (the exact bug:
    bytes drop to 0 in real time yet the call reports 'timed out').

    The freed keys don't come back, so the next list is empty and the job
    succeeds instead of falsely reporting failure.
    """
    client = _FakeS3Client(
        objects={"a": 1, "b": 1},
        delete_raises_times=1,
        delete_frees_before_raise=True,
    )
    progress = _ProgressRecorder()

    outcome = await run_clear_bucket(progress, client, "test-bucket")  # type: ignore[arg-type]

    assert outcome.success is True  # bucket IS empty; no false failure


# ---------------------------------------------------------------------------
# clear_stalled - a permanently stuck object
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permanently_stuck_object_stalls_reporting_deleted_so_far() -> None:
    client = _FakeS3Client(objects={"a": 1, "b": 1, "x": 1}, stuck_keys={"x"})
    progress = _ProgressRecorder()

    outcome = await run_clear_bucket(progress, client, "test-bucket")  # type: ignore[arg-type]

    assert outcome.success is False
    assert outcome.failure_reason == "clear_stalled"
    assert outcome.extras["deleted_count"] == 2  # a and b did go
    assert {e["Key"] for e in outcome.extras["errors"]} == {"x"}
    assert "Retry to continue" in outcome.extras["error"]


@pytest.mark.asyncio
async def test_stalled_errors_truncated_to_first_ten() -> None:
    stuck = {f"k{i}" for i in range(15)}
    client = _FakeS3Client(objects={k: 1 for k in stuck}, stuck_keys=stuck)
    progress = _ProgressRecorder()

    outcome = await run_clear_bucket(progress, client, "test-bucket")  # type: ignore[arg-type]

    assert outcome.failure_reason == "clear_stalled"
    assert outcome.extras["failed_count"] == 15
    assert len(outcome.extras["errors"]) == 10  # trimmed on the wire


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
# credential-less purge clear
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
            f"stormpulse.garage.jobs.clear_bucket.admin_api.{fn}",
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
        "stormpulse.garage.jobs.clear_bucket.GarageS3Client", fake_ctor,
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
    client = _FakeS3Client(objects={"a": 1, "b": 1})
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
    client = _FakeS3Client(objects={"a": 1})
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
    # Auth failure surfaces from the list (the credential proof), not a
    # separate HeadBucket; the temp key must still be destroyed after.
    client = _FakeS3Client(
        list_raises=S3AuthError("403 Forbidden", status=403),
    )
    _patch_s3_client(monkeypatch, client)

    outcome, _ = await _run_credential_less(fake)

    assert outcome.success is False
    assert outcome.failure_reason == "auth_failed"
    assert fake.names()[-1] == "delete_key"
    assert outcome.extras["credential_less"] is True


@pytest.mark.asyncio
async def test_credential_less_leak_logged_when_clear_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """The leaked-key trail must survive the raise path, not just the return path."""
    fake = _FakeAdmin()
    fake.delete_key_result = (False, "admin API down")
    _patch_admin(monkeypatch, fake)
    client = _FakeS3Client(list_raises=RuntimeError("mid-clear network blip"))
    _patch_s3_client(monkeypatch, client)

    with caplog.at_level("ERROR"), pytest.raises(RuntimeError):
        await _run_credential_less(fake)

    # Key delete was still attempted, and its failure is on the record.
    assert fake.names()[-1] == "delete_key"
    assert "GKPURGE" in caplog.text
    assert "could not be deleted" in caplog.text


@pytest.mark.asyncio
async def test_credential_less_leaked_key_is_loud_in_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeAdmin()
    fake.delete_key_result = (False, "admin API hiccup")
    _patch_admin(monkeypatch, fake)
    client = _FakeS3Client(objects={"a": 1})
    _patch_s3_client(monkeypatch, client)

    outcome, _ = await _run_credential_less(fake)

    # The clear itself succeeded; the leaked key rides the result loudly.
    assert outcome.success is True
    assert outcome.extras["manual_cleanup_required"] == [
        {"type": "key", "id": "GKPURGE"},
    ]


# ---------------------------------------------------------------------------
# Incomplete multipart uploads: an empty object list is not an empty bucket
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_reports_an_in_flight_upload_and_does_not_abort_it() -> None:
    """Objects drain; the upload is named, kept, and the fix is spelled out.

    Fail-safe KEEP. An upload seconds old is a live customer operation, so the
    ordinary clear reports it rather than destroying it.
    """
    client = _FakeS3Client(objects={"a.bin": 10}, uploads={"inflight.bin": "u-1"})
    progress = _ProgressRecorder()

    outcome = await run_clear_bucket(progress, client, "bucket")  # type: ignore[arg-type]

    assert outcome.success
    assert outcome.extras["unfinished_uploads"] == 1
    assert "incomplete multipart upload" in outcome.stdout
    assert "garage_bucket_cleanup_uploads" in outcome.stdout
    assert client.aborted == [], "the ordinary clear aborted a live upload"


@pytest.mark.asyncio
async def test_clear_with_no_uploads_reports_zero_and_stays_quiet() -> None:
    """The clean case must not grow a scary message."""
    client = _FakeS3Client(objects={"a.bin": 10})
    progress = _ProgressRecorder()

    outcome = await run_clear_bucket(progress, client, "bucket")  # type: ignore[arg-type]

    assert outcome.extras["unfinished_uploads"] == 0
    assert "incomplete" not in outcome.stdout
    assert outcome.stdout == "Cleared 1 object(s)"


@pytest.mark.asyncio
async def test_an_unreachable_upload_check_reports_unknown_not_zero() -> None:
    """A failed check must never read as "no uploads".

    Reporting zero here would be the same lie in a new place: the clear would
    claim a clean bucket on the strength of a check that did not run.
    """
    client = _FakeS3Client(
        objects={"a.bin": 10},
        uploads_list_raises=S3Error("ListMultipartUploads -> transport error"),
    )
    progress = _ProgressRecorder()

    outcome = await run_clear_bucket(progress, client, "bucket")  # type: ignore[arg-type]

    assert outcome.success
    assert outcome.extras["unfinished_uploads"] is None
    assert "could not check" in outcome.stdout
