"""Handler for ``garage_attach_account_key``.

Grant an account key access to an existing dashboard bucket: the
literal inverse of ``garage_detach_account_key``. Where detach does
``DenyBucketKey`` + alias-remove + grant-absent read-back, attach does
``AllowBucketKey`` (at a chosen tier) + alias-add + grant-present read-back.

It is a deliberate, password-gated widening of a root credential's reach
(enforced at the website endpoint), so the grant is least-privilege: the
caller picks ro / rw / owner. The read-back confirms the grant actually landed
before reporting success.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.garage import admin_api
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.tiers import ATTACH_TIER_PERMS

logger = logging.getLogger(__name__)


_TOTAL_STEPS = 3



def make_attach_account_key_handler(
    garage_config: GarageConfig,
    params: dict[str, str],
) -> JobHandler | None:
    """Build a JobHandler from runtime params.

    Required: ``bucket_id``, ``account_key_id``, ``local_alias``, ``tier``
    (one of ro/rw/owner). Returns ``None`` if any are missing or tier invalid.
    """
    required = ("bucket_id", "account_key_id", "local_alias", "tier")
    if not all(params.get(k) for k in required):
        logger.error(
            "garage_attach_account_key missing required params: %s",
            [k for k in required if not params.get(k)],
        )
        return None
    if params["tier"] not in ATTACH_TIER_PERMS:
        logger.error("garage_attach_account_key invalid tier: %s", params["tier"])
        return None

    bucket_id = params["bucket_id"]
    account_key_id = params["account_key_id"]
    local_alias = params["local_alias"]
    tier = params["tier"]

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_attach_account_key(
            progress=progress,
            garage_config=garage_config,
            bucket_id=bucket_id,
            account_key_id=account_key_id,
            local_alias=local_alias,
            tier=tier,
        )

    return handler


async def run_attach_account_key(
    progress: ProgressCallback,
    garage_config: GarageConfig,
    bucket_id: str,
    account_key_id: str,
    local_alias: str,
    tier: str,
) -> JobOutcome:
    """Grant the tier, add the alias, and confirm the grant is present."""
    started_at = time.monotonic()
    admin_url, admin_token = garage_config.admin_url, garage_config.admin_token
    if not (admin_url and admin_token):
        return _failure(
            failure_reason="admin_api_unconfigured",
            bucket_id=bucket_id, account_key_id=account_key_id, tier=tier,
            stderr=(
                "Garage admin API not configured (admin_url + admin_token); set "
                "[garage] admin_url and admin_token_file."
            ),
            started_at=started_at,
        )
    if tier not in ATTACH_TIER_PERMS:
        return _failure(
            failure_reason="invalid_tier",
            bucket_id=bucket_id, account_key_id=account_key_id, tier=tier,
            stderr=f"tier must be one of {tuple(ATTACH_TIER_PERMS)}, got {tier!r}",
            started_at=started_at,
        )
    read, write, owner = ATTACH_TIER_PERMS[tier]

    # ---- Step 1: AllowBucketKey at the chosen tier ----
    await progress("starting", 0, _TOTAL_STEPS, f"Granting {tier} access")
    ok, err = await asyncio.to_thread(
        admin_api.allow_bucket_key,
        admin_url=admin_url, admin_token=admin_token,
        bucket_ref=bucket_id, access_key_id=account_key_id,
        read=read, write=write, owner=owner,
    )
    if not ok:
        return _failure(
            failure_reason="grant_failed",
            bucket_id=bucket_id, account_key_id=account_key_id, tier=tier,
            stderr=err, started_at=started_at,
        )

    # ---- Step 1b: deny the complement so the grant is EXACTLY the tier ----
    # AllowBucketKey only widens. To make attach a precise SET, so a change-scope
    # can NARROW (e.g. owner -> ro on a re-attach), deny every permission bit the
    # tier excludes. The owner tier excludes nothing, so it denies nothing. The
    # deny op's own positive result is the authoritative confirmation the bits
    # were removed. (Slice 3.)
    if not (read and write and owner):
        ok, err = await asyncio.to_thread(
            admin_api.deny_bucket_key,
            admin_url=admin_url, admin_token=admin_token,
            bucket_ref=bucket_id, access_key_id=account_key_id,
            read=not read, write=not write, owner=not owner,
        )
        if not ok:
            return _failure(
                failure_reason="grant_narrow_failed",
                bucket_id=bucket_id, account_key_id=account_key_id, tier=tier,
                stderr=err, started_at=started_at,
            )

    # ---- Step 2: AddBucketAlias (local; cosmetic, best-effort) ----
    await progress("running", 1, _TOTAL_STEPS, "Attaching alias")
    alias_ok, _alias_err = await asyncio.to_thread(
        admin_api.add_bucket_alias_local,
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

    # ---- Step 3: read-back confirmation (grant-present proof) ----
    await progress("running", 2, _TOTAL_STEPS, "Confirming grant landed")
    kinfo, kerr = await asyncio.to_thread(
        admin_api.get_key_info,
        admin_url=admin_url, admin_token=admin_token, access_key_id=account_key_id,
    )
    if kinfo is None:
        return _failure(
            failure_reason="grant_unconfirmed",
            bucket_id=bucket_id, account_key_id=account_key_id, tier=tier,
            stderr=f"read-back GetKeyInfo failed: {kerr}",
            started_at=started_at,
        )
    if not _grant_present(kinfo, bucket_id, (read, write, owner)):
        return _failure(
            failure_reason="grant_not_present",
            bucket_id=bucket_id, account_key_id=account_key_id, tier=tier,
            stderr=(
                "AllowBucketKey returned success but the read-back does not show "
                "the account key holding the requested grant on the bucket."
            ),
            started_at=started_at,
        )

    await progress("finalizing", _TOTAL_STEPS, _TOTAL_STEPS, "Account key attached")
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"Account key {account_key_id} attached to {bucket_id[:16]} ({tier})",
        extras={
            "bucket_id": bucket_id[:16],
            "account_key_id": account_key_id,
            "tier": tier,
            "confirmed_attached": True,
            "alias_added": alias_ok,
            "manual_cleanup_required": manual,
            "garage_stderr": "",
            "duration_seconds": _elapsed(started_at),
        },
    )


def _grant_present(
    kinfo: dict[str, Any], bucket_id: str, want: tuple[bool, bool, bool],
) -> bool:
    """True if the key's read-back shows the bucket with at least the requested
    (read, write, owner) grant. ``bucket_id`` is the 16-char prefix."""
    wr, ww, wo = want
    for entry in kinfo.get("buckets") or []:
        full_id = entry.get("id") or ""
        if not full_id.startswith(bucket_id):
            continue
        perms = entry.get("permissions") or {}
        have_r, have_w, have_o = (
            bool(perms.get("read")), bool(perms.get("write")), bool(perms.get("owner")),
        )
        return (have_r or not wr) and (have_w or not ww) and (have_o or not wo)
    return False


def _failure(
    *,
    failure_reason: str,
    bucket_id: str,
    account_key_id: str,
    tier: str,
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
            "tier": tier,
            "confirmed_attached": False,
            "manual_cleanup_required": [],
            "garage_stderr": stderr,
            "duration_seconds": _elapsed(started_at),
        },
    )


def _elapsed(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)
