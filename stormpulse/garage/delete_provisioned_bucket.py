"""Handler for ``garage_delete_provisioned_bucket``.

Deletes an empty provisioned bucket via the admin HTTP API (ADR garage/001):
read the bucket (``GetBucketInfo``), ``DeleteBucket`` by id (which removes the
bucket together with ALL its aliases in one call), then best-effort cleanup of
any access keys the deletion left unmoored.

The CLI's temp-global-alias + detach-locals dance is gone: it only existed to
work around a Garage v2.2.0 CLI deadlock that the admin API does not have.
``DeleteBucket`` is the single mutation, so there is no rollback. Idempotent on
a missing bucket. Step 3 (key cleanup) is best-effort: failures accumulate in
``manual_cleanup_required`` rather than failing the orchestrator.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.garage import admin_api
from stormpulse.garage.config import GarageConfig

logger = logging.getLogger(__name__)


_TOTAL_STEPS = 3


def make_delete_provisioned_bucket_handler(
    garage_config: GarageConfig,
    params: dict[str, str],
) -> JobHandler | None:
    """Build a JobHandler from runtime params.

    Required: ``bucket_id``. Returns ``None`` if missing.
    """
    if not params.get("bucket_id"):
        logger.error(
            "garage_delete_provisioned_bucket missing required param: bucket_id",
        )
        return None
    bucket_id = params["bucket_id"]

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_delete_provisioned_bucket(
            progress=progress,
            garage_config=garage_config,
            bucket_id=bucket_id,
        )

    return handler


async def run_delete_provisioned_bucket(
    progress: ProgressCallback,
    garage_config: GarageConfig,
    bucket_id: str,
) -> JobOutcome:
    """Read, delete, and clean up. No rollback - DeleteBucket is atomic."""
    started_at = time.monotonic()
    admin_url, admin_token = garage_config.admin_url, garage_config.admin_token
    if not (admin_url and admin_token):
        # Fail loud: a migrated operation never silently no-ops (ADR garage/001).
        return _failure(
            failure_reason="admin_api_unconfigured",
            step_failed=None,
            bucket_id=bucket_id,
            step_completed=None,
            stderr=(
                "Garage admin API not configured (admin_url + admin_token); set "
                "[garage] admin_url and admin_token_file."
            ),
            started_at=started_at,
        )

    # ---- Step 1: GetBucketInfo ----
    await progress("starting", 0, _TOTAL_STEPS, "Reading bucket state")
    info, err = admin_api.get_bucket_info(
        admin_url=admin_url, admin_token=admin_token, bucket_ref=bucket_id,
    )
    if info is None:
        # Idempotent: a bucket that's already gone is success.
        if admin_api.is_not_found(err):
            return _already_gone(bucket_id, started_at)
        return _failure(
            failure_reason="bucket_info_failed",
            step_failed="bucket_info",
            bucket_id=bucket_id,
            step_completed=None,
            stderr=err,
            started_at=started_at,
        )
    full_id = info.get("id") or ""
    if int(info.get("objects") or 0) > 0:
        # Customer-actionable: surface it before any mutation.
        return _failure(
            failure_reason="bucket_not_empty",
            step_failed="bucket_info",
            bucket_id=bucket_id,
            step_completed=None,
            stderr=(
                f"Bucket has {int(info.get('objects') or 0)} object(s). "
                "Clear the bucket before deleting."
            ),
            started_at=started_at,
        )
    candidate_key_ids = [
        k.get("accessKeyId") for k in info.get("keys") or [] if k.get("accessKeyId")
    ]

    # ---- Step 2: DeleteBucket (removes the bucket and all its aliases) ----
    await progress("running", 1, _TOTAL_STEPS, "Deleting bucket")
    ok, err = admin_api.delete_bucket(
        admin_url=admin_url, admin_token=admin_token, bucket_ref=full_id or bucket_id,
    )
    if not ok:
        # A race (objects added after the check) surfaces as not-empty here too.
        reason = "bucket_not_empty" if "not empty" in err.lower() else "bucket_delete_failed"
        return _failure(
            failure_reason=reason,
            step_failed="bucket_delete",
            bucket_id=bucket_id,
            step_completed="bucket_info",
            stderr=err,
            started_at=started_at,
        )

    # ---- Step 3: clean up unmoored keys (best-effort) ----
    await progress("running", 2, _TOTAL_STEPS, "Cleaning up unmoored keys")
    manual, deleted, skipped = await _cleanup_unmoored_keys(
        garage_config, candidate_key_ids,
    )

    # ---- Success ----
    await progress("finalizing", _TOTAL_STEPS, _TOTAL_STEPS, "Bucket deleted")
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"Bucket {bucket_id[:16]} deleted",
        extras={
            "bucket_id": bucket_id[:16],
            "step_completed": "key_cleanup",
            "step_failed": None,
            "rollback_status": "not_required",
            "manual_cleanup_required": manual,
            "keys_deleted": deleted,
            "keys_skipped": skipped,
            "garage_stderr": "",
            "duration_seconds": _elapsed(started_at),
        },
    )


async def _cleanup_unmoored_keys(
    garage_config: GarageConfig,
    candidate_key_ids: list[str],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Delete each candidate key iff the deletion left it with no buckets.

    A key still attached to another bucket (shared key) is preserved. A key
    already gone is skipped. Any read/delete failure is best-effort: it goes to
    the manual-cleanup list, never raising (admin_api returns errors, not
    exceptions). Returns ``(manual_cleanup, keys_deleted, keys_skipped)``.
    """
    admin_url, admin_token = garage_config.admin_url, garage_config.admin_token
    manual: list[dict[str, Any]] = []
    deleted: list[str] = []
    skipped: list[str] = []
    for key_id in candidate_key_ids:
        kinfo, kerr = admin_api.get_key_info(
            admin_url=admin_url, admin_token=admin_token, access_key_id=key_id,
        )
        if kinfo is None:
            if admin_api.is_not_found(kerr):
                skipped.append(key_id)  # already gone, nothing to clean
            else:
                manual.append({"type": "key", "id": key_id})
            continue
        if kinfo.get("buckets"):
            skipped.append(key_id)  # shared key, still attached elsewhere
            continue
        ok, _err = admin_api.delete_key(
            admin_url=admin_url, admin_token=admin_token, access_key_id=key_id,
        )
        if ok:
            deleted.append(key_id)
        else:
            manual.append({"type": "key", "id": key_id})
    return manual, deleted, skipped


def _already_gone(bucket_id: str, started_at: float) -> JobOutcome:
    """The bucket already doesn't exist; this is success (idempotent)."""
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"Bucket {bucket_id[:16]} already absent",
        extras={
            "bucket_id": bucket_id[:16],
            "step_completed": "bucket_info",
            "step_failed": None,
            "rollback_status": "not_required",
            "manual_cleanup_required": [],
            "already_absent": True,
            "garage_stderr": "",
            "duration_seconds": _elapsed(started_at),
        },
    )


def _failure(
    *,
    failure_reason: str,
    step_failed: str | None,
    bucket_id: str,
    step_completed: str | None,
    stderr: str,
    started_at: float,
) -> JobOutcome:
    return JobOutcome(
        success=False,
        exit_code=-1,
        stdout="",
        stderr=stderr,
        failure_reason=failure_reason,
        extras={
            "bucket_id": bucket_id[:16],
            "step_completed": step_completed,
            "step_failed": step_failed,
            "rollback_status": "not_required",
            "manual_cleanup_required": [],
            "garage_stderr": stderr,
            "duration_seconds": _elapsed(started_at),
        },
    )


def _elapsed(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)
