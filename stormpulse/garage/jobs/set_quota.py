"""Handler for ``garage_bucket_set_quota`` via the Garage admin HTTP API.

The Headroom wall. The website dispatches this with ``bucket_id``
(the garage_bucket_id, never the local alias) and ``max_size`` in decimal bytes;
the handler POSTs ``UpdateBucket`` to set the bucket's max-size quota.

The admin token is the node's, resolved from config and bound in by the factory
builder, never sent over the wire (ADR buckets/000). If the admin API is not
configured, the handler fails loudly rather than leaving the bucket silently
uncapped, the lesson from the poisoned-anchor incident.
"""
from __future__ import annotations

import asyncio
import logging
import time

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.garage import admin_api

logger = logging.getLogger(__name__)


def make_set_quota_handler(
    params: dict[str, str], *, admin_url: str, admin_token: str,
) -> JobHandler | None:
    """Build a JobHandler for ``garage_bucket_set_quota``.

    Returns None (-> structured no-handler failure) if a required param is
    missing, ``max_size`` is not a non-negative integer, or the admin API is
    not configured.
    """
    bucket_id = params.get("bucket_id", "")
    raw_max = params.get("max_size", "")
    if not bucket_id or not raw_max:
        logger.error(
            "garage_bucket_set_quota missing required params: %s",
            [k for k in ("bucket_id", "max_size") if not params.get(k)],
        )
        return None
    try:
        max_size_bytes = int(raw_max)
    except (TypeError, ValueError):
        logger.error("garage_bucket_set_quota: max_size not an integer: %r", raw_max)
        return None
    if max_size_bytes < 0:
        logger.error("garage_bucket_set_quota: max_size must be >= 0, got %d", max_size_bytes)
        return None
    if not admin_url or not admin_token:
        logger.error(
            "garage_bucket_set_quota: Garage admin API not configured "
            "([garage] admin_url + admin_token/admin_token_file); cannot set quota"
        )
        return None

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_set_quota(
            progress,
            admin_url=admin_url,
            admin_token=admin_token,
            bucket_id=bucket_id,
            max_size_bytes=max_size_bytes,
        )

    return handler


async def run_set_quota(
    progress: ProgressCallback,
    *,
    admin_url: str,
    admin_token: str,
    bucket_id: str,
    max_size_bytes: int,
) -> JobOutcome:
    """POST UpdateBucket to set the bucket's max-size quota.

    Tests inject the admin_api call result; production hits the loopback
    admin endpoint.
    """
    started_at = time.monotonic()
    await progress("starting", 0, 1, "Setting bucket quota")

    ok, err = await asyncio.to_thread(
        admin_api.set_bucket_quota,
        admin_url=admin_url,
        admin_token=admin_token,
        bucket_id=bucket_id,
        max_size_bytes=max_size_bytes,
    )
    if not ok:
        return JobOutcome(
            success=False,
            exit_code=-1,
            stderr=f"UpdateBucket quota failed: {err}",
            failure_reason="os_error",
            extras={
                "duration_seconds": _elapsed(started_at),
                "error": err,
                "bucket_id": bucket_id,
                "max_size_bytes": max_size_bytes,
            },
        )

    await progress("finalizing", 1, 1, "Quota applied")
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"Set max-size quota {max_size_bytes} bytes on {bucket_id}",
        extras={
            "bucket_id": bucket_id,
            "max_size_bytes": max_size_bytes,
            "duration_seconds": _elapsed(started_at),
        },
    )


def _elapsed(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)
