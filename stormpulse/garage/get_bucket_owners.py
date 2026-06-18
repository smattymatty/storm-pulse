"""Handler for ``garage_get_bucket_owners``.

Read-only: return the access keys that own a bucket (via ``GetBucketInfo``), so
Storm can tell which account key is linked to a dashboard bucket (BUCKETS-013).
The inverse of ``garage_get_key_buckets``: Storm matches the returned key ids
against ``AccountKey`` rows for the bucket-detail provenance line ("created with
account key X"). No mutation; a missing bucket (404) returns an empty list.
"""

from __future__ import annotations

import logging
import time

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.garage import admin_api
from stormpulse.garage.config import GarageConfig

logger = logging.getLogger(__name__)


def make_get_bucket_owners_handler(
    garage_config: GarageConfig,
    params: dict[str, str],
) -> JobHandler | None:
    """Build a JobHandler from runtime params. Required: ``bucket_id``."""
    if not params.get("bucket_id"):
        logger.error("garage_get_bucket_owners missing required param: bucket_id")
        return None
    bucket_id = params["bucket_id"]

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_get_bucket_owners(
            progress=progress, garage_config=garage_config, bucket_id=bucket_id,
        )

    return handler


async def run_get_bucket_owners(
    progress: ProgressCallback,
    garage_config: GarageConfig,
    bucket_id: str,
) -> JobOutcome:
    """Return the access-key ids that hold owner on the bucket."""
    started_at = time.monotonic()
    admin_url, admin_token = garage_config.admin_url, garage_config.admin_token
    if not (admin_url and admin_token):
        return _failure(
            failure_reason="admin_api_unconfigured",
            bucket_id=bucket_id,
            stderr=(
                "Garage admin API not configured (admin_url + admin_token); set "
                "[garage] admin_url and admin_token_file."
            ),
            started_at=started_at,
        )

    await progress("starting", 0, 1, "Reading bucket owners")
    info, err = admin_api.get_bucket_info(
        admin_url=admin_url, admin_token=admin_token, bucket_ref=bucket_id,
    )
    if info is None:
        if admin_api.is_not_found(err):
            return _success(bucket_id, owner_key_ids=[], started_at=started_at)
        return _failure(
            failure_reason="bucket_read_failed",
            bucket_id=bucket_id, stderr=err, started_at=started_at,
        )

    owner_key_ids: list[str] = []
    for entry in info.get("keys") or []:
        kid = entry.get("accessKeyId") or ""
        perms = entry.get("permissions") or {}
        if kid and perms.get("owner"):
            owner_key_ids.append(kid)
    return _success(bucket_id, owner_key_ids=owner_key_ids, started_at=started_at)


def _success(
    bucket_id: str, *, owner_key_ids: list[str], started_at: float,
) -> JobOutcome:
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"Bucket {bucket_id[:16]} has {len(owner_key_ids)} owner key(s)",
        extras={
            "bucket_id": bucket_id[:16],
            "owner_key_ids": owner_key_ids,
            "garage_stderr": "",
            "duration_seconds": _elapsed(started_at),
        },
    )


def _failure(
    *, failure_reason: str, bucket_id: str, stderr: str, started_at: float,
) -> JobOutcome:
    return JobOutcome(
        success=False,
        exit_code=-1,
        stdout="",
        stderr=stderr,
        failure_reason=failure_reason,
        extras={
            "bucket_id": bucket_id[:16],
            "owner_key_ids": [],
            "garage_stderr": stderr,
            "duration_seconds": _elapsed(started_at),
        },
    )


def _elapsed(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)
