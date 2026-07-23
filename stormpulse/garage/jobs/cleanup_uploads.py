"""Handler for ``garage_bucket_cleanup_uploads`` via the Garage admin HTTP API.

Reclaims disk held by multipart uploads that were started and never completed.
Those parts are resident on the node but invisible to ListObjectsV2, and Garage
does not count them against the bucket's quota: verified against v2.3.0, an
ordinary PUT past a bucket's max-size is refused 403 while a multipart part
past the same cap is accepted 200. That makes incomplete uploads storage the
cap does not govern, so there has to be a way to reclaim it deliberately.

``older_than_secs`` is the safety, not a tuning knob. An upload started seconds
ago is a live customer operation; one from days ago is garbage. Aborting by age
keeps the fail-safe direction: an upload too young to classify as garbage is
kept, never aborted. There is deliberately no "abort everything" mode here, and
the parameter has no default, so a caller must state the age it means.

The admin token is the node's, resolved from config and bound in by the factory
builder, never sent over the wire (ADR buckets/000).
"""
from __future__ import annotations

import asyncio
import logging
import time

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.garage import admin_api

logger = logging.getLogger(__name__)


def make_cleanup_uploads_handler(
    params: dict[str, str], *, admin_url: str, admin_token: str,
) -> JobHandler | None:
    """Build a JobHandler for ``garage_bucket_cleanup_uploads``.

    Returns None (-> structured no-handler failure) if a required param is
    missing, ``older_than_secs`` is not a non-negative integer, or the admin
    API is not configured.
    """
    bucket_id = params.get("bucket_id", "")
    raw_age = params.get("older_than_secs", "")
    if not bucket_id or not raw_age:
        logger.error(
            "garage_bucket_cleanup_uploads missing required params: %s",
            [k for k in ("bucket_id", "older_than_secs") if not params.get(k)],
        )
        return None
    try:
        older_than_secs = int(raw_age)
    except (TypeError, ValueError):
        logger.error(
            "garage_bucket_cleanup_uploads: older_than_secs not an integer: %r",
            raw_age,
        )
        return None
    if older_than_secs < 0:
        logger.error(
            "garage_bucket_cleanup_uploads: older_than_secs must be >= 0, got %d",
            older_than_secs,
        )
        return None
    if not admin_url or not admin_token:
        logger.error(
            "garage_bucket_cleanup_uploads: Garage admin API not configured "
            "([garage] admin_url + admin_token/admin_token_file); cannot abort "
            "incomplete uploads"
        )
        return None

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_cleanup_uploads(
            progress,
            admin_url=admin_url,
            admin_token=admin_token,
            bucket_id=bucket_id,
            older_than_secs=older_than_secs,
        )

    return handler


async def run_cleanup_uploads(
    progress: ProgressCallback,
    *,
    admin_url: str,
    admin_token: str,
    bucket_id: str,
    older_than_secs: int,
) -> JobOutcome:
    """POST CleanupIncompleteUploads and report how many uploads were aborted."""
    started_at = time.monotonic()
    await progress("starting", 0, 1, "Aborting incomplete uploads")

    deleted, err = await asyncio.to_thread(
        admin_api.cleanup_incomplete_uploads,
        admin_url=admin_url,
        admin_token=admin_token,
        bucket_ref=bucket_id,
        older_than_secs=older_than_secs,
    )
    if deleted is None:
        return JobOutcome(
            success=False,
            exit_code=-1,
            stderr=f"CleanupIncompleteUploads failed: {err}",
            failure_reason="os_error",
            extras={
                "duration_seconds": _elapsed(started_at),
                "error": err,
                "bucket_id": bucket_id,
                "older_than_secs": older_than_secs,
            },
        )

    await progress("finalizing", 1, 1, "Incomplete uploads aborted")
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=(
            f"Aborted {deleted} incomplete upload(s) older than "
            f"{older_than_secs}s in {bucket_id}"
        ),
        extras={
            "bucket_id": bucket_id,
            "older_than_secs": older_than_secs,
            "uploads_aborted": deleted,
            "duration_seconds": _elapsed(started_at),
        },
    )


def _elapsed(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)
