"""Handler for ``garage_delete_customer_key``.

Guarded delete for a per-bucket customer key: the key dies only if the
bucket keeps at least one *covering* key. The control plane names which
keys count as coverage (``covering_key_ids``); this handler verifies one
of them actually holds a live grant on the bucket in Garage right now,
then deletes with the same confirmed-gone semantics as
``garage_delete_key``.

The split matters: which keys qualify as coverage is control-plane
policy, but whether a grant exists is a fact only Garage knows. Like the
stranding pre-pass in ``enforce_account_key_tier``, the check runs
against real Garage state via GetBucketInfo, never against stored rows,
and the job is all-or-nothing: a failed coverage check
(``not_covered``) makes no changes at all.

Two deliberate edges:

- **Presence is not coverage.** A detached key can linger in a bucket's
  key list with every permission denied; coverage requires at least one
  live permission (read, write, or owner).
- **A positively absent bucket satisfies the guard vacuously.** The
  guard protects the bucket's remaining access; a bucket that is gone
  from Garage has none to protect, and blocking would strand a zombie
  key that only this command can kill. Reported as ``bucket_absent`` in
  the outcome, never silently.

The coverage check and the delete are adjacent admin calls, not a
transaction. The window between them is milliseconds, and the only way
to lose the race is the same operator detaching the covering key in
parallel; the delete itself remains confirmed-gone either way.

All Garage interaction is the admin HTTP API, never the CLI.
"""

from __future__ import annotations

import asyncio
import logging
import time

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.garage import admin_api
from stormpulse.garage.config import GarageConfig

logger = logging.getLogger(__name__)


_TOTAL_STEPS = 2


def make_delete_customer_key_handler(
    garage_config: GarageConfig,
    params: dict[str, str],
) -> JobHandler | None:
    """Build a JobHandler from runtime params.

    Required: ``key_id``, ``bucket_id``, ``covering_key_ids`` (comma-separated
    Garage key ids). Returns ``None`` if any is missing.
    """
    key_id = params.get("key_id")
    bucket_id = params.get("bucket_id")
    covering_raw = params.get("covering_key_ids")
    if not key_id or not bucket_id or not covering_raw:
        logger.error(
            "garage_delete_customer_key missing required param(s): "
            "key_id, bucket_id and covering_key_ids are all required"
        )
        return None
    covering_key_ids = [k for k in covering_raw.split(",") if k]

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_delete_customer_key(
            progress=progress,
            garage_config=garage_config,
            key_id=key_id,
            bucket_id=bucket_id,
            covering_key_ids=covering_key_ids,
        )

    return handler


async def run_delete_customer_key(
    progress: ProgressCallback,
    garage_config: GarageConfig,
    key_id: str,
    bucket_id: str,
    covering_key_ids: list[str],
) -> JobOutcome:
    """Verify coverage against live Garage state, then delete the key."""
    started_at = time.monotonic()
    admin_url, admin_token = garage_config.admin_url, garage_config.admin_token
    if not (admin_url and admin_token):
        return _failure(
            failure_reason="admin_api_unconfigured",
            key_id=key_id, bucket_id=bucket_id,
            stderr=(
                "Garage admin API not configured (admin_url + admin_token); set "
                "[garage] admin_url and admin_token_file."
            ),
            started_at=started_at,
        )

    # ---- Step 1: coverage check against live bucket state ----
    await progress("starting", 0, _TOTAL_STEPS, "Checking coverage")
    binfo, berr = await asyncio.to_thread(
        admin_api.get_bucket_info,
        admin_url=admin_url, admin_token=admin_token, bucket_ref=bucket_id,
    )
    covered_by = ""
    bucket_absent = False
    if binfo is None:
        if admin_api.is_not_found(berr):
            # Positive 404: the bucket is gone, so there is no remaining
            # access to protect. Vacuously covered; proceed to the delete.
            bucket_absent = True
        else:
            # Transport / 5xx / auth: cannot verify coverage, so make no
            # changes. Fail closed and let the caller retry.
            return _failure(
                failure_reason="coverage_check_failed",
                key_id=key_id, bucket_id=bucket_id,
                stderr=f"GetBucketInfo failed: {berr}",
                started_at=started_at,
            )
    else:
        wanted = set(covering_key_ids)
        for entry in binfo.get("keys") or []:
            access_key_id = entry.get("accessKeyId") or ""
            if access_key_id not in wanted:
                continue
            p = entry.get("permissions") or {}
            if p.get("read") or p.get("write") or p.get("owner"):
                covered_by = access_key_id
                break
        if not covered_by:
            return _failure(
                failure_reason="not_covered",
                key_id=key_id, bucket_id=bucket_id,
                stderr=(
                    "Refusing to delete: none of the covering keys hold a "
                    "live grant on this bucket. Attach one first, then retry."
                ),
                started_at=started_at,
            )

    # ---- Step 2: delete, confirmed-gone (same contract as delete_key) ----
    await progress("running", 1, _TOTAL_STEPS, "Deleting key")
    ok, err = await asyncio.to_thread(
        admin_api.delete_key,
        admin_url=admin_url, admin_token=admin_token, access_key_id=key_id,
    )
    if ok:
        return _confirmed(
            key_id, bucket_id, "deleted",
            covered_by=covered_by, bucket_absent=bucket_absent,
            started_at=started_at,
        )
    if admin_api.is_not_found(err):
        # Positive 404 / NoSuchKey: already gone. Idempotent success.
        return _confirmed(
            key_id, bucket_id, "already_absent",
            covered_by=covered_by, bucket_absent=bucket_absent,
            started_at=started_at,
        )
    # Transport / 5xx / auth after the guard passed: do NOT certify the key
    # as gone. The caller keeps its kill-intent open and retries.
    return _failure(
        failure_reason="key_delete_failed",
        key_id=key_id, bucket_id=bucket_id, stderr=err,
        started_at=started_at,
        covered_by=covered_by, bucket_absent=bucket_absent,
        guard_passed=True,
    )


def _confirmed(
    key_id: str,
    bucket_id: str,
    outcome: str,
    *,
    covered_by: str,
    bucket_absent: bool,
    started_at: float,
) -> JobOutcome:
    """The key is positively gone (deleted or already absent)."""
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"Key {key_id} {outcome}",
        extras={
            "key_id": key_id,
            "bucket_id": bucket_id,
            "outcome": outcome,
            "confirmed_absent": True,
            "guard_passed": True,
            "covered_by": covered_by,
            "bucket_absent": bucket_absent,
            "garage_stderr": "",
            "duration_seconds": _elapsed(started_at),
        },
    )


def _failure(
    *,
    failure_reason: str,
    key_id: str,
    bucket_id: str,
    stderr: str,
    started_at: float,
    covered_by: str = "",
    bucket_absent: bool = False,
    guard_passed: bool = False,
) -> JobOutcome:
    return JobOutcome(
        success=False,
        exit_code=-1,
        stdout="",
        stderr=stderr,
        failure_reason=failure_reason,
        extras={
            "key_id": key_id,
            "bucket_id": bucket_id,
            "outcome": "transient_error" if guard_passed else "no_change",
            "confirmed_absent": False,
            "guard_passed": guard_passed,
            "covered_by": covered_by,
            "bucket_absent": bucket_absent,
            "garage_stderr": stderr,
            "duration_seconds": _elapsed(started_at),
        },
    )


def _elapsed(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)
