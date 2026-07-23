"""Incomplete multipart uploads: storage the quota does not govern.

The hole this file pins, measured against Garage v2.3.0:

- An ordinary PUT past a bucket's max-size quota is refused (403).
- A multipart PART past the same quota is accepted (200).
- Those parts are resident on disk, invisible to ListObjectsV2, and reported
  by the admin API only under ``unfinishedMultipartUpload*``.
- DeleteBucket does NOT refuse a bucket holding only such parts.

So a customer can park unbounded bytes outside their cap, and before this
work the agent could not see them, did not report them, and told the customer
"cleared" while they remained.

None of that is a Garage bug to file; it is Garage's quota semantics. It is a
capacity fact Storm has to account for, which is exactly what a wire test is
for. If a future Garage starts counting MPU bytes against the quota, the
enforcement test here goes red and that is good news worth reading.
"""

from __future__ import annotations

import pytest

from stormpulse.garage import admin_api
from stormpulse.garage.jobs.cleanup_uploads import run_cleanup_uploads
from stormpulse.garage.s3 import GarageS3Client
from tests.wire.garage.conftest import (
    WireBucket,
    WireEnv,
    abort_upload,
    put_object,
    start_upload,
    upload_part,
)


class _ProgressRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, int, int | None, str]] = []

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
        self.events.append((stage, current, total, message))


# ---------------------------------------------------------------------------
# The hole itself
# ---------------------------------------------------------------------------


def test_an_ordinary_put_past_the_quota_is_refused(
    wire: WireEnv, bucket: WireBucket
) -> None:
    """The cap works on the ordinary path. The control for the next test."""
    ok, err = admin_api.set_bucket_quota(
        **wire.admin_kwargs, bucket_id=bucket.id, max_size_bytes=1_048_576
    )
    assert ok, err

    with pytest.raises(Exception) as caught:
        put_object(wire, bucket.name, "too-big.bin", b"B" * 3_000_000)
    assert "403" in str(caught.value), caught.value


def test_a_multipart_part_past_the_quota_is_accepted(
    wire: WireEnv, bucket: WireBucket
) -> None:
    """The cap does NOT hold on the multipart path.

    The finding, pinned. If this ever fails, Garage started enforcing the
    quota against multipart parts and the capacity model gained a guarantee it
    never had.
    """
    ok, err = admin_api.set_bucket_quota(
        **wire.admin_kwargs, bucket_id=bucket.id, max_size_bytes=1_048_576
    )
    assert ok, err

    upload_id = start_upload(wire, bucket.name, "sneaky.bin")
    status = upload_part(wire, bucket.name, "sneaky.bin", upload_id, b"C" * 8_000_000)
    assert status == 200, (
        f"expected the MPU part past the cap to be accepted (the known hole), "
        f"got HTTP {status}"
    )

    info, err = admin_api.get_bucket_info(**wire.admin_kwargs, bucket_ref=bucket.id)
    assert err == "", err
    assert info is not None
    assert info["bytes"] == 0, "accounted bytes should not include MPU parts"
    assert info["unfinishedMultipartUploadBytes"] == 8_000_000

    abort_upload(wire, bucket.name, "sneaky.bin", upload_id)


def test_incomplete_uploads_are_invisible_to_the_object_list(
    wire: WireEnv, s3: GarageS3Client, bucket: WireBucket
) -> None:
    """ListObjectsV2 reports nothing, which is why an empty list proves nothing."""
    upload_id = start_upload(wire, bucket.name, "hidden.bin")
    upload_part(wire, bucket.name, "hidden.bin", upload_id, b"D" * 200_000)

    assert s3.list_objects_v2(bucket.name, max_keys=1000).contents == []

    listing = s3.list_multipart_uploads(bucket.name)
    assert [u.key for u in listing.uploads] == ["hidden.bin"]

    abort_upload(wire, bucket.name, "hidden.bin", upload_id)


# ---------------------------------------------------------------------------
# See it: the walk carries the bytes
# ---------------------------------------------------------------------------


def test_the_walk_reports_unfinished_upload_bytes(
    wire: WireEnv, bucket: WireBucket
) -> None:
    """``unfinished_upload_bytes`` rides on the state push, separate from usage.

    Separate on purpose. Folding it into ``size_bytes`` would redefine what
    usage means to the capacity model, which is a sealed decision, not a
    reporting detail. This surfaces the bytes so the control plane can see
    storage the cap does not govern.
    """
    from stormpulse.garage.state import collect_garage_state

    upload_id = start_upload(wire, bucket.name, "parked.bin")
    upload_part(wire, bucket.name, "parked.bin", upload_id, b"E" * 500_000)

    state = collect_garage_state(wire.garage_config())
    assert state is not None
    found = next(b for b in state.buckets if b.alias == bucket.name)

    assert found.unfinished_upload_bytes == 500_000
    assert found.unfinished_uploads == 1
    assert found.size_bytes == 0, (
        "MPU bytes must NOT be folded into size_bytes; that is the sealed "
        "capacity-model question, not this field's to answer"
    )

    abort_upload(wire, bucket.name, "parked.bin", upload_id)


# ---------------------------------------------------------------------------
# Stop lying about it: clear reports what it could not remove
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clear_does_not_claim_empty_while_uploads_hold_data(
    wire: WireEnv, s3: GarageS3Client, bucket: WireBucket
) -> None:
    """A clear over a bucket with an in-flight upload says so, and does not abort it.

    Fail-safe KEEP: an upload seconds old is a live customer operation. The
    clear removes every object, reports the upload it left alone, and names
    the command that would reclaim it.
    """
    from stormpulse.garage.jobs.clear_bucket import run_clear_bucket

    put_object(wire, bucket.name, "real.bin", b"F" * 1000)
    upload_id = start_upload(wire, bucket.name, "inflight.bin")
    upload_part(wire, bucket.name, "inflight.bin", upload_id, b"G" * 300_000)

    progress = _ProgressRecorder()
    outcome = await run_clear_bucket(progress, s3, bucket.name)

    assert outcome.success
    assert outcome.extras["unfinished_uploads"] == 1, outcome.extras
    assert "incomplete multipart upload" in outcome.stdout, outcome.stdout
    assert "garage_bucket_cleanup_uploads" in outcome.stdout, outcome.stdout

    # It reported the upload; it did not abort it.
    still_there = s3.list_multipart_uploads(bucket.name)
    assert len(still_there.uploads) == 1, "clear aborted a live upload"

    abort_upload(wire, bucket.name, "inflight.bin", upload_id)


@pytest.mark.asyncio
async def test_clear_of_a_genuinely_empty_bucket_reports_no_uploads(
    s3: GarageS3Client, bucket: WireBucket
) -> None:
    """The clean case still reads clean: no scary message when there is nothing."""
    from stormpulse.garage.jobs.clear_bucket import run_clear_bucket

    progress = _ProgressRecorder()
    outcome = await run_clear_bucket(progress, s3, bucket.name)

    assert outcome.success
    assert outcome.extras["unfinished_uploads"] == 0
    assert "incomplete" not in outcome.stdout, outcome.stdout


# ---------------------------------------------------------------------------
# Clean it: the operator command, against the real endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_uploads_aborts_and_frees_the_bytes(
    wire: WireEnv, bucket: WireBucket
) -> None:
    """The reclaim path end to end: bytes resident, then gone."""
    upload_id = start_upload(wire, bucket.name, "garbage.bin")
    upload_part(wire, bucket.name, "garbage.bin", upload_id, b"H" * 400_000)

    info, err = admin_api.get_bucket_info(**wire.admin_kwargs, bucket_ref=bucket.id)
    assert err == "", err
    assert info is not None
    assert info["unfinishedMultipartUploadBytes"] == 400_000

    progress = _ProgressRecorder()
    outcome = await run_cleanup_uploads(
        progress,
        admin_url=wire.admin_url,
        admin_token=wire.admin_token,
        bucket_id=bucket.id,
        older_than_secs=0,
    )
    assert outcome.success, outcome.stderr
    assert outcome.extras["uploads_aborted"] == 1, outcome.extras

    info, err = admin_api.get_bucket_info(**wire.admin_kwargs, bucket_ref=bucket.id)
    assert err == "", err
    assert info is not None
    assert info["unfinishedMultipartUploadBytes"] == 0


@pytest.mark.asyncio
async def test_cleanup_uploads_keeps_an_upload_younger_than_the_cutoff(
    wire: WireEnv, bucket: WireBucket
) -> None:
    """The age bound is real: a young upload survives a cleanup that names an age.

    This is the fail-safe direction. If the cutoff were ignored, every cleanup
    would destroy in-flight customer uploads, and it would look like success.
    """
    upload_id = start_upload(wire, bucket.name, "young.bin")
    upload_part(wire, bucket.name, "young.bin", upload_id, b"I" * 100_000)

    progress = _ProgressRecorder()
    outcome = await run_cleanup_uploads(
        progress,
        admin_url=wire.admin_url,
        admin_token=wire.admin_token,
        bucket_id=bucket.id,
        older_than_secs=3600,
    )
    assert outcome.success, outcome.stderr
    assert outcome.extras["uploads_aborted"] == 0, (
        "cleanup aborted an upload younger than its own cutoff"
    )

    info, err = admin_api.get_bucket_info(**wire.admin_kwargs, bucket_ref=bucket.id)
    assert err == "", err
    assert info is not None
    assert info["unfinishedMultipartUploadBytes"] == 100_000

    abort_upload(wire, bucket.name, "young.bin", upload_id)


# ---------------------------------------------------------------------------
# The purge path is allowed to abort
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_purge_clear_aborts_uploads_the_ordinary_clear_keeps(
    wire: WireEnv, bucket: WireBucket
) -> None:
    """A credential-less purge leaves nothing resident.

    The bucket is being destroyed, so there is no live operation to protect
    and parts left behind would be bytes nobody can reach or account for. The
    same upload an ordinary clear preserves is aborted here.
    """
    from stormpulse.garage.jobs.clear_bucket import run_clear_bucket_credential_less

    put_object(wire, bucket.name, "doomed.bin", b"J" * 2000)
    upload_id = start_upload(wire, bucket.name, "doomed-upload.bin")
    upload_part(wire, bucket.name, "doomed-upload.bin", upload_id, b"K" * 250_000)

    progress = _ProgressRecorder()
    outcome = await run_clear_bucket_credential_less(
        progress=progress,
        garage_config=wire.garage_config(),
        bucket_id=bucket.id,
        endpoint=wire.s3_endpoint,
        region=wire.region,
    )
    assert outcome.success, outcome.stderr
    assert outcome.extras["uploads_aborted"] == 1, outcome.extras

    info, err = admin_api.get_bucket_info(**wire.admin_kwargs, bucket_ref=bucket.id)
    assert err == "", err
    assert info is not None
    assert info["unfinishedMultipartUploadBytes"] == 0, (
        "the purge left multipart bytes resident in a bucket being destroyed"
    )
    assert info["objects"] == 0


@pytest.mark.asyncio
async def test_the_purge_clear_leaves_no_temporary_key_behind(
    wire: WireEnv, bucket: WireBucket
) -> None:
    """The purge's minted key is destroyed, and the outcome names which one.

    A leaked purge key holds read/write on a customer bucket. The mint happens
    before a ``finally`` that always deletes it; this is the only place that
    claim is checked against a real key store.
    """
    from stormpulse.garage.jobs.clear_bucket import run_clear_bucket_credential_less

    put_object(wire, bucket.name, "x.bin", b"L" * 10)

    progress = _ProgressRecorder()
    outcome = await run_clear_bucket_credential_less(
        progress=progress,
        garage_config=wire.garage_config(),
        bucket_id=bucket.id,
        endpoint=wire.s3_endpoint,
        region=wire.region,
    )
    assert outcome.success, outcome.stderr
    purge_key_id = outcome.extras["purge_key_id"]
    assert purge_key_id, outcome.extras

    info, err = admin_api.get_key_info(
        **wire.admin_kwargs, access_key_id=purge_key_id
    )
    assert info is None, f"the purge key {purge_key_id} survived the clear"
    assert admin_api.is_not_found(err), err
    assert outcome.extras["manual_cleanup_required"] == []
