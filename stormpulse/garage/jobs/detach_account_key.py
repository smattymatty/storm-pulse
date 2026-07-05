"""Handler for ``garage_detach_account_key``.

Removes one account key's grant on a single bucket:

  1. DenyBucketKey - revoke the key's read/write/owner on the bucket.
  2. RemoveBucketAlias (local) - drop the key's name alias for the bucket;
     the bucket stays named via the admin key's alias (claim-then-detach).
  3. Read-back - GetKeyInfo and confirm the bucket is gone from the key's
     grant list.

This is grant-removal, NOT key-destruction: the account key survives and
keeps its other buckets. Per a later amendment, detach is
confirmed by the deny op's own positive result plus a grant-absent read-back
inside this same operation, never by a 404 and never by a reconcile snapshot.

The deny is the security-critical step. The alias drop is cosmetic, so a
failed alias drop is flagged for manual cleanup but does not fail an
otherwise-confirmed detach.
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


def make_detach_account_key_handler(
    garage_config: GarageConfig,
    params: dict[str, str],
) -> JobHandler | None:
    """Build a JobHandler from runtime params.

    Required: ``bucket_id``, ``account_key_id``, ``local_alias``. Returns
    ``None`` if any are missing.
    """
    required = ("bucket_id", "account_key_id", "local_alias")
    if not all(params.get(k) for k in required):
        logger.error(
            "garage_detach_account_key missing required params: %s",
            [k for k in required if not params.get(k)],
        )
        return None

    bucket_id = params["bucket_id"]
    account_key_id = params["account_key_id"]
    local_alias = params["local_alias"]

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_detach_account_key(
            progress=progress,
            garage_config=garage_config,
            bucket_id=bucket_id,
            account_key_id=account_key_id,
            local_alias=local_alias,
        )

    return handler


async def run_detach_account_key(
    progress: ProgressCallback,
    garage_config: GarageConfig,
    bucket_id: str,
    account_key_id: str,
    local_alias: str,
) -> JobOutcome:
    """Deny the grant, drop the alias, and confirm the grant is gone."""
    started_at = time.monotonic()
    admin_url, admin_token = garage_config.admin_url, garage_config.admin_token
    if not (admin_url and admin_token):
        # Fail loud: a migrated operation never silently no-ops.
        return _failure(
            failure_reason="admin_api_unconfigured",
            bucket_id=bucket_id,
            account_key_id=account_key_id,
            stderr=(
                "Garage admin API not configured (admin_url + admin_token); set "
                "[garage] admin_url and admin_token_file."
            ),
            started_at=started_at,
        )

    # ---- Step 1: DenyBucketKey (the security-critical step) ----
    await progress("starting", 0, _TOTAL_STEPS, "Revoking account-key grant")
    ok, err = admin_api.deny_bucket_key(
        admin_url=admin_url, admin_token=admin_token,
        bucket_ref=bucket_id, access_key_id=account_key_id,
        read=True, write=True, owner=True,
    )
    if not ok:
        return _failure(
            failure_reason="grant_revoke_failed",
            bucket_id=bucket_id,
            account_key_id=account_key_id,
            stderr=err,
            started_at=started_at,
        )

    # ---- Step 2: RemoveBucketAlias (cosmetic; best-effort) ----
    await progress("running", 1, _TOTAL_STEPS, "Dropping account-key alias")
    alias_ok, alias_err = admin_api.remove_bucket_alias_local(
        admin_url=admin_url, admin_token=admin_token,
        bucket_ref=bucket_id, access_key_id=account_key_id,
        local_alias=local_alias,
    )
    manual: list[dict[str, Any]] = []
    if not alias_ok:
        manual.append({
            "type": "local_alias",
            "key_id": account_key_id, "alias": local_alias,
        })

    # ---- Step 3: read-back confirmation (the Q1 grant-absent proof) ----
    await progress("running", 2, _TOTAL_STEPS, "Confirming grant removed")
    kinfo, kerr = admin_api.get_key_info(
        admin_url=admin_url, admin_token=admin_token,
        access_key_id=account_key_id,
    )
    if kinfo is None:
        # The key itself being gone (404) also means the grant is gone, but
        # detach should never destroy the key; treat an unreadable key as
        # unconfirmed rather than asserting success.
        return _failure(
            failure_reason="grant_absence_unconfirmed",
            bucket_id=bucket_id,
            account_key_id=account_key_id,
            stderr=f"read-back GetKeyInfo failed: {kerr}",
            started_at=started_at,
        )
    if _still_granted(kinfo, bucket_id):
        return _failure(
            failure_reason="grant_still_present",
            bucket_id=bucket_id,
            account_key_id=account_key_id,
            stderr=(
                "DenyBucketKey returned success but the read-back still shows "
                "the account key holding a grant on the bucket."
            ),
            started_at=started_at,
        )

    # ---- Success: grant confirmed gone ----
    await progress("finalizing", _TOTAL_STEPS, _TOTAL_STEPS, "Account key detached")
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"Account key {account_key_id} detached from {bucket_id[:16]}",
        extras={
            "bucket_id": bucket_id[:16],
            "account_key_id": account_key_id,
            "confirmed_detached": True,
            "alias_removed": alias_ok,
            "manual_cleanup_required": manual,
            "garage_stderr": "",
            "duration_seconds": _elapsed(started_at),
        },
    )


def _still_granted(kinfo: dict[str, Any], bucket_id: str) -> bool:
    """True if the key's read-back still shows any permission on the bucket.

    Grant-absent means the bucket is either gone from the key's ``buckets``
    list, or present with every permission false. ``bucket_id`` is Storm's
    16-char prefix; Garage's entries carry the full id, matched by prefix.
    """
    for entry in kinfo.get("buckets") or []:
        full_id = entry.get("id") or ""
        if not full_id.startswith(bucket_id):
            continue
        perms = entry.get("permissions") or {}
        return any(bool(perms.get(p)) for p in ("read", "write", "owner"))
    return False


def _failure(
    *,
    failure_reason: str,
    bucket_id: str,
    account_key_id: str,
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
            "account_key_id": account_key_id,
            "confirmed_detached": False,
            "manual_cleanup_required": [],
            "garage_stderr": stderr,
            "duration_seconds": _elapsed(started_at),
        },
    )


def _elapsed(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)
