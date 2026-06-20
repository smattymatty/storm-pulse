"""Handler for ``garage_enforce_account_key_tier`` (BUCKETS-016 Slice 4).

Enforce the invariant "an account key's per-bucket grants never exceed its
tier." Reads the key's actual grants from Garage, and for every bucket where
the grant sits above the tier, narrows it with a precise set (allow the tier's
permissions, deny the rest), the same primitive attach uses.

Two disciplines make this safe to run on demand and from a cron backstop:

- **All-or-nothing on stranding.** Before changing anything, every bucket whose
  owner grant would be removed is checked for ANOTHER owner via GetBucketInfo.
  If removing this key's owner would leave a bucket with no owner at all, the
  whole job aborts and reports the at-risk buckets, making no changes. The
  customer claims a per-bucket admin key on each, then retries. The check runs
  against real Garage state, not Storm's stored rows.
- **Idempotent.** Re-running when every grant is already at or below the tier is
  a no-op, so the eager dispatch (on a tier change) and the cron backstop call
  the same handler against the same target (the tier) without drift.

All Garage interaction is the admin HTTP API (ADR garage/001), never the CLI.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.garage import admin_api
from stormpulse.garage.config import GarageConfig

logger = logging.getLogger(__name__)

# (read, write, owner) the tier allows as its maximum per-bucket grant.
_TIER_MAX: dict[str, tuple[bool, bool, bool]] = {
    "all": (True, True, True),
    "rw": (True, True, False),
    "ro": (True, False, False),
}


def make_enforce_account_key_tier_handler(
    garage_config: GarageConfig,
    params: dict[str, str],
) -> JobHandler | None:
    """Build a JobHandler. Required params: ``account_key_id``, ``tier``."""
    account_key_id = params.get("account_key_id")
    tier = params.get("tier")
    if not account_key_id:
        logger.error("garage_enforce_account_key_tier missing required param: account_key_id")
        return None
    if tier not in _TIER_MAX:
        logger.error("garage_enforce_account_key_tier invalid tier: %s", tier)
        return None

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_enforce_account_key_tier(
            progress=progress,
            garage_config=garage_config,
            account_key_id=account_key_id,
            tier=tier,
        )

    return handler


def _exceeds(grant: tuple[bool, bool, bool], tier_max: tuple[bool, bool, bool]) -> bool:
    """True if the key's grant holds a permission the tier forbids. Read is
    always allowed, so only write and owner can exceed."""
    _gr, gw, go = grant
    _mr, mw, mo = tier_max
    return (gw and not mw) or (go and not mo)


async def run_enforce_account_key_tier(
    progress: ProgressCallback,
    garage_config: GarageConfig,
    account_key_id: str,
    tier: str,
) -> JobOutcome:
    """Narrow every over-tier grant down to the tier. All-or-nothing on
    stranding; idempotent when already enforced."""
    started_at = time.monotonic()
    admin_url, admin_token = garage_config.admin_url, garage_config.admin_token
    if not (admin_url and admin_token):
        return _failure(
            failure_reason="admin_api_unconfigured", account_key_id=account_key_id,
            tier=tier, stderr=(
                "Garage admin API not configured (admin_url + admin_token); set "
                "[garage] admin_url and admin_token_file."
            ),
            started_at=started_at,
        )
    if tier not in _TIER_MAX:
        return _failure(
            failure_reason="invalid_tier", account_key_id=account_key_id, tier=tier,
            stderr=f"tier must be one of {tuple(_TIER_MAX)}, got {tier!r}",
            started_at=started_at,
        )
    tier_max = _TIER_MAX[tier]
    mr, mw, mo = tier_max

    # ---- Step 1: read the key's actual grants ----
    await progress("starting", 0, 3, "Reading current grants")
    kinfo, kerr = admin_api.get_key_info(
        admin_url=admin_url, admin_token=admin_token, access_key_id=account_key_id,
    )
    if kinfo is None:
        return _failure(
            failure_reason="read_failed", account_key_id=account_key_id, tier=tier,
            stderr=f"GetKeyInfo failed: {kerr}", started_at=started_at,
        )

    over_tier: list[tuple[str, tuple[bool, bool, bool]]] = []
    for entry in kinfo.get("buckets") or []:
        bid = entry.get("id") or ""
        if not bid:
            continue
        p = entry.get("permissions") or {}
        grant = (bool(p.get("read")), bool(p.get("write")), bool(p.get("owner")))
        if _exceeds(grant, tier_max):
            over_tier.append((bid, grant))

    if not over_tier:
        # Already enforced. Idempotent no-op (the common cron-backstop case).
        return _success(account_key_id, tier, narrowed=[], started_at=started_at)

    # ---- Step 2: stranding pre-pass (ALL-OR-NOTHING, no changes yet) ----
    # For every over-tier bucket whose owner grant we'd remove, confirm another
    # owner exists. If any would be left ownerless, abort the whole job.
    await progress("running", 1, 3, "Checking for stranding")
    strand_risk: list[str] = []
    for bid, (_gr, _gw, go) in over_tier:
        if not (go and not mo):
            continue  # not removing owner here; can't strand
        binfo, berr = admin_api.get_bucket_info(
            admin_url=admin_url, admin_token=admin_token, bucket_ref=bid,
        )
        if binfo is None:
            return _failure(
                failure_reason="strand_check_failed", account_key_id=account_key_id,
                tier=tier, stderr=f"GetBucketInfo failed for {bid[:16]}: {berr}",
                started_at=started_at,
            )
        other_owner = any(
            (k.get("accessKeyId") or "") != account_key_id
            and (k.get("permissions") or {}).get("owner")
            for k in (binfo.get("keys") or [])
        )
        if not other_owner:
            strand_risk.append(bid[:16])

    if strand_risk:
        # Block the whole demote: no changes made. The customer claims an admin
        # key on each, then retries.
        return _failure(
            failure_reason="would_strand", account_key_id=account_key_id, tier=tier,
            stderr=(
                "Refusing to narrow: these buckets would be left with no owner. "
                "Claim an admin key on each, then retry: "
                + ", ".join(strand_risk)
            ),
            started_at=started_at, strand_risk=strand_risk,
        )

    # ---- Step 3: narrow each over-tier grant to exactly the tier ----
    await progress("running", 2, 3, "Narrowing grants")
    narrowed: list[str] = []
    for bid, _grant in over_tier:
        ok, err = admin_api.allow_bucket_key(
            admin_url=admin_url, admin_token=admin_token,
            bucket_ref=bid, access_key_id=account_key_id,
            read=mr, write=mw, owner=mo,
        )
        if not ok:
            return _failure(
                failure_reason="narrow_allow_failed", account_key_id=account_key_id,
                tier=tier, stderr=f"AllowBucketKey {bid[:16]}: {err}",
                started_at=started_at, narrowed=narrowed,
            )
        # owner tier (all) denies nothing; rw/ro deny the complement.
        if not (mr and mw and mo):
            ok, err = admin_api.deny_bucket_key(
                admin_url=admin_url, admin_token=admin_token,
                bucket_ref=bid, access_key_id=account_key_id,
                read=not mr, write=not mw, owner=not mo,
            )
            if not ok:
                return _failure(
                    failure_reason="narrow_deny_failed", account_key_id=account_key_id,
                    tier=tier, stderr=f"DenyBucketKey {bid[:16]}: {err}",
                    started_at=started_at, narrowed=narrowed,
                )
        narrowed.append(bid[:16])

    await progress("finalizing", 3, 3, "Grants enforced")
    return _success(account_key_id, tier, narrowed=narrowed, started_at=started_at)


def _success(
    account_key_id: str, tier: str, *, narrowed: list[str], started_at: float,
) -> JobOutcome:
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"Enforced tier {tier} on {account_key_id}: narrowed {len(narrowed)} bucket(s)",
        extras={
            "account_key_id": account_key_id,
            "tier": tier,
            "narrowed_buckets": narrowed,
            "garage_stderr": "",
            "duration_seconds": _elapsed(started_at),
        },
    )


def _failure(
    *,
    failure_reason: str,
    account_key_id: str,
    tier: str,
    stderr: str,
    started_at: float,
    narrowed: list[str] | None = None,
    strand_risk: list[str] | None = None,
) -> JobOutcome:
    return JobOutcome(
        success=False,
        exit_code=-1,
        stdout="",
        stderr=stderr,
        failure_reason=failure_reason,
        extras={
            "account_key_id": account_key_id,
            "tier": tier,
            "narrowed_buckets": narrowed or [],
            "strand_risk": strand_risk or [],
            "garage_stderr": stderr,
            "duration_seconds": _elapsed(started_at),
        },
    )


def _elapsed(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)
