"""Handler for the ``garage_bucket_clear`` long-running command.

The dashboard dispatches a ``command.request`` with the customer's admin
S3 secret in the params. This handler:

1. Constructs a ``GarageS3Client`` with those credentials. The secret
   lives in agent process memory only for the duration of the job; it is
   never persisted, never logged, and dropped when the function returns.
2. Validates the credentials with a HeadBucket pre-flight. Bad creds
   produce a clean ``failure_reason="auth_failed"`` outcome before any
   delete happens.
3. Paginates the bucket contents to compute an accurate total. Until the
   list is complete, progress events report ``total=None``.
4. Deletes in batches of 1000 (the S3 DeleteObjects upper bound),
   inspecting per-object errors after each batch — this is the bug class
   that the Django path got wrong (200 OK with non-empty ``Errors`` array
   silently treated as success). The contract here is strict: any
   per-object error fails the whole job with ``failure_reason="partial_failure"``.
5. Emits ``stage="finalizing"`` while computing the summary, then
   returns a ``JobOutcome`` carrying the summary as extras.

Failure modes and their ``failure_reason`` values:

- ``auth_failed``      — HeadBucket returned 403 / SignatureDoesNotMatch.
- ``partial_failure``  — DeleteObjects reported per-object errors. The
                          dashboard treats this as overall failure (P1
                          contract): bucket counts stay where they were.
- ``os_error``         — list/delete request failed at HTTP level
                          (network, server error, etc.).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.garage.s3 import (
    GarageS3Client,
    S3AuthError,
    S3Error,
)

logger = logging.getLogger(__name__)


_BATCH_SIZE = 1000  # S3 DeleteObjects accepts at most 1000 keys per call.
_MAX_REPORTED_ERRORS = 10  # Trim the errors array on the wire to keep messages small.


# ---------------------------------------------------------------------------
# Public entrypoint — called by agent.py to wire into JobManager
# ---------------------------------------------------------------------------


def make_clear_bucket_handler(params: dict[str, str]) -> JobHandler | None:
    """Build a JobHandler for ``garage_bucket_clear`` from runtime params.

    Returns None if a required param is missing — the caller emits a
    structured no-handler failure rather than crashing.
    """
    required = ("bucket_name", "s3_endpoint", "region", "access_key_id", "secret_access_key")
    if not all(params.get(k) for k in required):
        logger.error(
            "garage_bucket_clear missing required params: %s",
            [k for k in required if not params.get(k)],
        )
        return None

    bucket = params["bucket_name"]
    endpoint = params["s3_endpoint"]
    region = params["region"]
    access_key = params["access_key_id"]
    secret_key = params["secret_access_key"]

    try:
        client = GarageS3Client(
            endpoint=endpoint,
            region=region,
            access_key=access_key,
            secret_key=secret_key,
        )
    except ValueError:
        logger.exception("Failed to construct GarageS3Client for clear_bucket")
        return None

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_clear_bucket(progress, client, bucket)

    return handler


# ---------------------------------------------------------------------------
# Core logic — directly testable with a fake client
# ---------------------------------------------------------------------------


async def run_clear_bucket(
    progress: ProgressCallback,
    client: GarageS3Client,
    bucket: str,
) -> JobOutcome:
    """Clear all objects from ``bucket`` via the local Garage S3 endpoint.

    Tests inject a fake ``GarageS3Client``; production wires the real one.
    """
    started_at = time.monotonic()

    # ---- Phase 0: credential pre-flight ----
    await progress("starting", 0, None, "Validating credentials")
    try:
        await asyncio.to_thread(client.head_bucket, bucket)
    except S3AuthError as exc:
        return JobOutcome(
            success=False,
            exit_code=-1,
            stderr=f"Authentication failed: {exc}",
            failure_reason="auth_failed",
            extras={
                "deleted_count": 0,
                "failed_count": 0,
                "errors": [],
                "duration_seconds": _elapsed(started_at),
                "error": "Could not authenticate. Check your Admin secret key.",
            },
        )
    except S3Error as exc:
        return JobOutcome(
            success=False,
            exit_code=-1,
            stderr=f"Bucket pre-flight failed: {exc}",
            failure_reason="os_error",
            extras={
                "deleted_count": 0,
                "failed_count": 0,
                "errors": [],
                "duration_seconds": _elapsed(started_at),
                "error": str(exc),
            },
        )

    # ---- Phase 1: paginate the bucket to compute total ----
    await progress("starting", 0, None, "Listing objects")
    all_keys: list[str] = []
    continuation: str | None = None
    while True:
        try:
            page = await asyncio.to_thread(
                client.list_objects_v2, bucket, continuation,
            )
        except S3Error as exc:
            return JobOutcome(
                success=False,
                exit_code=-1,
                stderr=f"List failed: {exc}",
                failure_reason="os_error",
                extras={
                    "deleted_count": 0,
                    "failed_count": 0,
                    "errors": [],
                    "duration_seconds": _elapsed(started_at),
                    "error": str(exc),
                },
            )
        all_keys.extend(o.key for o in page.contents)
        if not page.is_truncated:
            break
        continuation = page.next_continuation_token

    total = len(all_keys)
    if total == 0:
        # Nothing to delete — succeed with zero counts.
        return JobOutcome(
            success=True,
            exit_code=0,
            stdout="Bucket is already empty",
            extras={
                "deleted_count": 0,
                "failed_count": 0,
                "errors": [],
                "duration_seconds": _elapsed(started_at),
            },
        )

    # ---- Phase 2: delete in batches, emitting per-batch progress ----
    deleted_total = 0
    error_entries: list[dict[str, str]] = []
    for i in range(0, total, _BATCH_SIZE):
        batch = all_keys[i:i + _BATCH_SIZE]
        try:
            result = await asyncio.to_thread(
                client.delete_objects, bucket, batch,
            )
        except S3Error as exc:
            return JobOutcome(
                success=False,
                exit_code=-1,
                stderr=f"Delete batch failed: {exc}",
                failure_reason="os_error",
                extras={
                    "deleted_count": deleted_total,
                    "failed_count": total - deleted_total,
                    "errors": error_entries[:_MAX_REPORTED_ERRORS],
                    "duration_seconds": _elapsed(started_at),
                    "error": str(exc),
                },
            )
        deleted_total += len(result.deleted)
        for err in result.errors:
            error_entries.append(
                {"Key": err.key, "Code": err.code, "Message": err.message},
            )
        await progress(
            "running", deleted_total, total,
            f"Deleted {deleted_total} of {total}",
        )

    # ---- Phase 3: finalize and report ----
    await progress("finalizing", deleted_total, total, "Computing summary")

    if error_entries:
        # P1 contract: any per-object failure means the whole job failed.
        # The dashboard will leave the bucket counts untouched and let the
        # customer retry.
        return JobOutcome(
            success=False,
            exit_code=-1,
            stderr=f"{len(error_entries)} object(s) could not be deleted",
            failure_reason="partial_failure",
            extras={
                "deleted_count": deleted_total,
                "failed_count": len(error_entries),
                "errors": error_entries[:_MAX_REPORTED_ERRORS],
                "duration_seconds": _elapsed(started_at),
                "error": (
                    f"{len(error_entries)} of {total} objects could not be deleted. "
                    "The bucket was partially cleared; retry to finish."
                ),
            },
        )

    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"Cleared {deleted_total} object(s)",
        extras={
            "deleted_count": deleted_total,
            "failed_count": 0,
            "errors": [],
            "duration_seconds": _elapsed(started_at),
        },
    )


def _elapsed(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)
