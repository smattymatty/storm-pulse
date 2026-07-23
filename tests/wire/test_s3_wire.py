"""The S3 data plane against a real Garage: SigV4, pagination, and the drain.

The agent's S3 client is read-and-delete only, and the delete path is the one
that destroys customer data. ``tests/garage/test_clear_bucket.py`` proves the
loop converges against a stateful fake; this file proves it converges against
Garage, including the parts a fake cannot model (real SigV4 over the wire,
real pagination, real DeleteObjects batching semantics).

Promoted from the old ``tests/garage/test_s3_integration.py``, which skipped
itself on four unset env vars and therefore never ran.
"""

from __future__ import annotations

import pytest

from stormpulse.garage.jobs.clear_bucket import run_clear_bucket
from stormpulse.garage.s3 import GarageS3Client, S3AuthError
from tests.wire.conftest import (
    WireBucket,
    WireEnv,
    put_object,
    put_object_with_declared_hash,
)

# ---------------------------------------------------------------------------
# SigV4: the signature has to be right on the wire, not just in a unit test
# ---------------------------------------------------------------------------


def test_head_bucket_succeeds_with_valid_credentials(
    s3: GarageS3Client, bucket: WireBucket
) -> None:
    """The credential proof the agent runs before every clear. No exception."""
    s3.head_bucket(bucket.name)


def test_head_bucket_rejects_a_wrong_secret_as_S3AuthError(
    wire: WireEnv, bucket: WireBucket
) -> None:
    """A bad secret is a typed auth error, never a generic crash.

    ``run_clear_bucket`` branches on S3AuthError to report ``auth_failed``
    rather than a stalled clear, so the classification is load-bearing for
    what the operator sees.
    """
    bad = GarageS3Client(
        endpoint=wire.s3_endpoint,
        region=wire.region,
        access_key=wire.access_key,
        secret_key="not-the-real-secret-key-deliberately-wrong",
    )
    with pytest.raises(S3AuthError):
        bad.head_bucket(bucket.name)


def test_head_bucket_on_a_missing_bucket_raises(s3: GarageS3Client) -> None:
    """A nonexistent bucket is an error, never a silent success."""
    with pytest.raises(Exception):
        s3.head_bucket("wire-definitely-not-a-real-bucket")


# ---------------------------------------------------------------------------
# ListObjectsV2: the shape the drain loop walks
# ---------------------------------------------------------------------------


def test_list_objects_v2_returns_keys_and_exact_sizes(
    wire: WireEnv, s3: GarageS3Client, bucket: WireBucket
) -> None:
    """Contents carry the key and the exact byte size the agent sums."""
    put_object(wire, bucket.name, "one.bin", b"a" * 100)
    put_object(wire, bucket.name, "two.bin", b"b" * 250)

    result = s3.list_objects_v2(bucket.name, max_keys=10)
    sizes = {entry.key: entry.size for entry in result.contents}
    assert sizes == {"one.bin": 100, "two.bin": 250}, sizes
    assert result.key_count == 2
    assert result.is_truncated is False


def test_list_objects_v2_paginates_with_a_continuation_token(
    wire: WireEnv, s3: GarageS3Client, bucket: WireBucket
) -> None:
    """Truncation and the continuation token behave as the loop assumes.

    The drain loop re-lists from the front rather than paginating, but the
    detector and usage walk both depend on truncation being reported
    correctly. A silently-untruncated page under-reports every large bucket.
    """
    for i in range(5):
        put_object(wire, bucket.name, f"page-{i}.bin", b"x" * 10)

    first = s3.list_objects_v2(bucket.name, max_keys=2)
    assert len(first.contents) == 2
    assert first.is_truncated is True
    assert first.next_continuation_token

    second = s3.list_objects_v2(
        bucket.name, continuation_token=first.next_continuation_token, max_keys=2
    )
    assert len(second.contents) == 2
    first_keys = {e.key for e in first.contents}
    second_keys = {e.key for e in second.contents}
    assert not (first_keys & second_keys), "pages overlapped"


def test_list_objects_v2_on_an_empty_bucket_is_empty_not_an_error(
    s3: GarageS3Client, bucket: WireBucket
) -> None:
    """An empty list is the drain loop's proof of completion.

    If Garage ever errored instead of returning an empty page, every clear
    would report stalled on its final round.
    """
    result = s3.list_objects_v2(bucket.name, max_keys=1000)
    assert result.contents == []
    assert result.key_count == 0
    assert result.is_truncated is False


# ---------------------------------------------------------------------------
# DeleteObjects: HTTP 200 with per-object errors is the trap
# ---------------------------------------------------------------------------


def test_delete_objects_reports_deleted_keys(
    wire: WireEnv, s3: GarageS3Client, bucket: WireBucket
) -> None:
    """The batch delete reports what it removed, with no per-object errors."""
    put_object(wire, bucket.name, "gone-1.bin", b"q" * 8)
    put_object(wire, bucket.name, "gone-2.bin", b"q" * 8)

    result = s3.delete_objects(bucket.name, ["gone-1.bin", "gone-2.bin"])
    assert result.errors == [], result.errors
    assert set(result.deleted) == {"gone-1.bin", "gone-2.bin"}
    assert s3.list_objects_v2(bucket.name, max_keys=10).contents == []


def test_delete_objects_on_an_absent_key_reports_NoSuchKey(
    s3: GarageS3Client, bucket: WireBucket
) -> None:
    """Garage DIVERGES from AWS S3 here, and the drain loop must not care.

    AWS S3 treats DeleteObjects on a missing key as success. Garage returns a
    per-object ``NoSuchKey`` error inside an HTTP 200. Pinned deliberately: the
    drain loop re-deletes keys whenever a response is lost, so if it measured
    progress by "no errors" instead of by "deleted_total advanced", every clear
    that dropped a response would report stalled. It measures the latter, which
    is why this divergence is survivable.

    If a future Garage adopts AWS semantics this test goes red. That is a
    behavior change worth reading, not a regression: relax it then.
    """
    result = s3.delete_objects(bucket.name, ["never-existed.bin"])
    assert [e.code for e in result.errors] == ["NoSuchKey"], result.errors
    assert result.deleted == [], result.deleted


# ---------------------------------------------------------------------------
# The drain loop, end to end, against real objects
# ---------------------------------------------------------------------------


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


@pytest.mark.asyncio
async def test_clear_drains_a_real_bucket_and_reports_bytes_freed(
    wire: WireEnv, s3: GarageS3Client, bucket: WireBucket
) -> None:
    """The whole purge path against Garage: seed, clear, verify empty.

    Twelve objects of known size across multiple list rounds. Asserts the
    bucket is actually empty afterwards (the customer-visible outcome) and
    that the reported ``bytes_freed`` matches what was seeded exactly, since
    that number rides to the dashboard.
    """
    seeded = 0
    for i in range(12):
        body = b"d" * (100 + i)
        put_object(wire, bucket.name, f"drain-{i:02d}.bin", body)
        seeded += len(body)

    progress = _ProgressRecorder()
    outcome = await run_clear_bucket(progress, s3, bucket.name)

    assert outcome.success, f"clear failed: {outcome}"
    assert s3.list_objects_v2(bucket.name, max_keys=1000).contents == [], (
        "clear reported success but objects remain"
    )
    freed = [ev[4] for ev in progress.events if ev[4] is not None]
    assert freed, f"no bytes_freed ever reported: {progress.events}"
    assert max(freed) == seeded, f"reported {max(freed)} freed, seeded {seeded}"


@pytest.mark.asyncio
async def test_clear_on_an_already_empty_bucket_succeeds(
    s3: GarageS3Client, bucket: WireBucket
) -> None:
    """Clearing an empty bucket is a clean success, not a stall.

    The idempotent re-run case: an operator retrying a clear that already
    finished must not see a failure.
    """
    progress = _ProgressRecorder()
    outcome = await run_clear_bucket(progress, s3, bucket.name)
    assert outcome.success, f"clearing an empty bucket failed: {outcome}"


@pytest.mark.asyncio
async def test_clear_with_bad_credentials_reports_auth_failed(
    wire: WireEnv, bucket: WireBucket
) -> None:
    """A credential failure is named ``auth_failed``, not a generic stall.

    The self-diagnosing-error rule: the operator reading the JobOutcome must
    learn what to fix from it.
    """
    bad = GarageS3Client(
        endpoint=wire.s3_endpoint,
        region=wire.region,
        access_key=wire.access_key,
        secret_key="wrong-secret-on-purpose-for-the-auth-branch",
    )
    progress = _ProgressRecorder()
    outcome = await run_clear_bucket(progress, bad, bucket.name)

    assert not outcome.success
    assert "auth" in repr(outcome).lower(), repr(outcome)


# ---------------------------------------------------------------------------
# Substrate property the agent leans on
# ---------------------------------------------------------------------------


def test_garage_rejects_a_body_whose_declared_hash_does_not_match(
    wire: WireEnv, bucket: WireBucket
) -> None:
    """A mismatched ``x-amz-content-sha256`` is refused, not silently stored.

    Every signed request the agent sends declares its payload hash, and
    DeleteObjects is a signed POST carrying a body that names customer
    objects. This pins that the substrate enforces body integrity rather than
    trusting the declaration, which is what makes the signature cover the
    payload and not just the headers.

    Found while mutation-testing the SigV4 path: corrupting the empty-body
    hash constant survived, because bodyless requests give Garage nothing to
    cross-check. With a real body it checks, and that is the case that
    matters.
    """
    status = put_object_with_declared_hash(
        wire, bucket.name, "tampered.bin", b"hello", "0" * 64
    )
    assert status == 400, f"Garage accepted a mismatched payload hash: HTTP {status}"
