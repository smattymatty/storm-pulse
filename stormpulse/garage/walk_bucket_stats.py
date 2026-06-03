"""Handler for ``garage_walk_bucket_stats``.

Counts objects and bytes under a prefix by paginating ListObjectsV2 against
the local Garage S3 endpoint. The customer's transient secret rides in
dispatch params; signing on loopback means the access log records the
agent's local IP rather than the customer's. Without this, every folder
navigation in the dashboard's file browser would pollute the customer's
activity feed.

Short-running, but uses the long-running plumbing because that's the
dispatch path the dashboard uses for result fan-out via buckets_relay.

Failure reasons: ``auth_failed``, ``os_error``. Truncation at
``max_objects`` returns ``truncated=True`` with the partial count + bytes.
"""

from __future__ import annotations

import asyncio
import logging
import time

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.garage.s3 import (
    GarageS3Client,
    S3AuthError,
    S3Error,
)

logger = logging.getLogger(__name__)


_DEFAULT_MAX_OBJECTS = 100_000  # mirrors stormbuckets._STATS_MAX_OBJECTS


def make_walk_bucket_stats_handler(params: dict[str, str]) -> JobHandler | None:
    """Build a JobHandler for ``garage_walk_bucket_stats`` from runtime params.

    Returns None if a required param is missing - the dispatcher emits
    a structured no-handler failure rather than crashing.
    """
    required = (
        "bucket_name",
        "s3_endpoint",
        "region",
        "access_key_id",
        "secret_access_key",
    )
    if not all(params.get(k) for k in required):
        logger.error(
            "garage_walk_bucket_stats missing required params: %s",
            [k for k in required if not params.get(k)],
        )
        return None

    bucket = params["bucket_name"]
    endpoint = params["s3_endpoint"]
    region = params["region"]
    access_key = params["access_key_id"]
    secret_key = params["secret_access_key"]
    # Empty prefix is legal - bucket-root walk.
    prefix = params.get("prefix", "") or ""
    try:
        max_objects = int(params.get("max_objects", "") or _DEFAULT_MAX_OBJECTS)
    except ValueError:
        max_objects = _DEFAULT_MAX_OBJECTS
    if max_objects <= 0:
        max_objects = _DEFAULT_MAX_OBJECTS

    try:
        client = GarageS3Client(
            endpoint=endpoint,
            region=region,
            access_key=access_key,
            secret_key=secret_key,
        )
    except ValueError:
        logger.exception("Failed to construct GarageS3Client for walk_bucket_stats")
        return None

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_walk_bucket_stats(
            progress,
            client,
            bucket,
            prefix,
            max_objects,
        )

    return handler


async def run_walk_bucket_stats(
    progress: ProgressCallback,
    client: GarageS3Client,
    bucket: str,
    prefix: str,
    max_objects: int,
) -> JobOutcome:
    """Walk ``bucket`` under ``prefix`` and sum object count + bytes.

    Paginates until either the listing is exhausted or ``max_objects``
    is reached, whichever comes first. Tests inject a fake client.
    """
    started_at = time.monotonic()

    # Auth pre-flight. Cheaper to fail here with a clean reason than
    # to surface the first list page's 403 as an opaque os_error.
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
                "count": 0,
                "bytes": 0,
                "truncated": False,
                "duration_seconds": _elapsed(started_at),
                "error": "Could not authenticate. Check the key/secret pair.",
            },
        )
    except S3Error as exc:
        return JobOutcome(
            success=False,
            exit_code=-1,
            stderr=f"Bucket pre-flight failed: {exc}",
            failure_reason="os_error",
            extras={
                "count": 0,
                "bytes": 0,
                "truncated": False,
                "duration_seconds": _elapsed(started_at),
                "error": str(exc),
            },
        )

    # Paginate and accumulate. Caller wants a recursive count under
    # ``prefix`` (no delimiter), so every object in every "subfolder"
    # is one tick on the counter.
    count = 0
    total_bytes = 0
    continuation: str | None = None
    truncated = False
    while True:
        try:
            page = await asyncio.to_thread(
                client.list_objects_v2,
                bucket,
                continuation,
                1000,
                prefix or None,
            )
        except S3Error as exc:
            return JobOutcome(
                success=False,
                exit_code=-1,
                stderr=f"List failed: {exc}",
                failure_reason="os_error",
                extras={
                    "count": count,
                    "bytes": total_bytes,
                    "truncated": False,
                    "duration_seconds": _elapsed(started_at),
                    "error": str(exc),
                },
            )

        for obj in page.contents:
            count += 1
            total_bytes += obj.size
            if count >= max_objects:
                truncated = True
                break

        if truncated or not page.is_truncated:
            break
        continuation = page.next_continuation_token

    await progress("finalizing", count, None, "Computing summary")
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"Walked {count} object(s) under prefix={prefix!r}",
        extras={
            "count": count,
            "bytes": total_bytes,
            "truncated": truncated,
            "duration_seconds": _elapsed(started_at),
        },
    )


def _elapsed(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)
