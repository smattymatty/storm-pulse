"""Handler for ``garage_converge_account_key_rotation``.

One idempotent convergence pass of an account-key rotation (BUCKETS-013):
grant the NEW account key owner + local alias on every bucket the OLD key
still owns that the new key does not yet own. Run via the admin token, so the
old key's (possibly lost) secret is never needed.

The pass is the unit of self-heal: Storm re-dispatches it each tick until it
reports ``converged`` (a pass that finds nothing left to transfer). Granting an
owner the key already holds is a no-op, so repeated passes are safe. This is
4a, purely additive: the new key co-owns everything; removing the old key's
access and reaping it is 4b.

If the old key is already gone (404), there is nothing to transfer and the
rotation is converged. The new key must exist; an unreadable new key is a
transient failure, retried next tick.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.config import GarageConfig
from stormpulse.garage import admin_api

logger = logging.getLogger(__name__)


_TOTAL_STEPS = 2


def make_converge_account_key_rotation_handler(
    garage_config: GarageConfig,
    params: dict[str, str],
) -> JobHandler | None:
    """Build a JobHandler from runtime params.

    Required: ``old_key_id``, ``new_key_id``. Returns ``None`` if missing.
    """
    required = ("old_key_id", "new_key_id")
    if not all(params.get(k) for k in required):
        logger.error(
            "garage_converge_account_key_rotation missing params: %s",
            [k for k in required if not params.get(k)],
        )
        return None
    old_key_id = params["old_key_id"]
    new_key_id = params["new_key_id"]

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_converge_account_key_rotation(
            progress=progress,
            garage_config=garage_config,
            old_key_id=old_key_id,
            new_key_id=new_key_id,
        )

    return handler


def _owned(kinfo: dict[str, Any]) -> dict[str, list[str]]:
    """Map ``full bucket id -> localAliases`` for every bucket this key owns."""
    owned: dict[str, list[str]] = {}
    for entry in kinfo.get("buckets") or []:
        full_id = entry.get("id") or ""
        perms = entry.get("permissions") or {}
        if full_id and perms.get("owner"):
            owned[full_id] = list(entry.get("localAliases") or [])
    return owned


async def run_converge_account_key_rotation(
    progress: ProgressCallback,
    garage_config: GarageConfig,
    old_key_id: str,
    new_key_id: str,
) -> JobOutcome:
    """Transfer ownership of the old key's buckets to the new key, once."""
    started_at = time.monotonic()
    admin_url, admin_token = garage_config.admin_url, garage_config.admin_token
    if not (admin_url and admin_token):
        return _failure(
            failure_reason="admin_api_unconfigured",
            old_key_id=old_key_id, new_key_id=new_key_id,
            stderr=(
                "Garage admin API not configured (admin_url + admin_token); set "
                "[garage] admin_url and admin_token_file."
            ),
            started_at=started_at,
        )

    # ---- Step 1: read both keys' current ownership ----
    await progress("starting", 0, _TOTAL_STEPS, "Reading rotation state")
    old_info, old_err = admin_api.get_key_info(
        admin_url=admin_url, admin_token=admin_token, access_key_id=old_key_id,
    )
    if old_info is None:
        if admin_api.is_not_found(old_err):
            # Old key already gone: nothing left to transfer, converged.
            return _converged(old_key_id, new_key_id, transferred=[], started_at=started_at)
        return _failure(
            failure_reason="old_key_read_failed",
            old_key_id=old_key_id, new_key_id=new_key_id,
            stderr=old_err, started_at=started_at,
        )
    new_info, new_err = admin_api.get_key_info(
        admin_url=admin_url, admin_token=admin_token, access_key_id=new_key_id,
    )
    if new_info is None:
        return _failure(
            failure_reason="new_key_read_failed",
            old_key_id=old_key_id, new_key_id=new_key_id,
            stderr=new_err, started_at=started_at,
        )

    old_owned = _owned(old_info)
    new_owned = set(_owned(new_info))
    to_transfer = [bid for bid in old_owned if bid not in new_owned]

    if not to_transfer:
        # A pass that finds nothing left to transfer is the converged signal.
        return _converged(old_key_id, new_key_id, transferred=[], started_at=started_at)

    # ---- Step 2: grant the new key owner + alias on each pending bucket ----
    await progress("running", 1, _TOTAL_STEPS, f"Transferring {len(to_transfer)} bucket(s)")
    transferred: list[str] = []
    manual: list[dict[str, Any]] = []
    for full_id in to_transfer:
        ok, err = admin_api.allow_bucket_key(
            admin_url=admin_url, admin_token=admin_token,
            bucket_ref=full_id, access_key_id=new_key_id,
            read=True, write=True, owner=True,
        )
        if not ok:
            manual.append({"type": "owner_grant", "bucket_id": full_id[:16], "error": err})
            continue
        aliases = old_owned[full_id]
        if aliases:
            # Cosmetic: replicate the old key's name for the bucket on the new key.
            admin_api.add_bucket_alias_local(
                admin_url=admin_url, admin_token=admin_token,
                bucket_ref=full_id, access_key_id=new_key_id,
                local_alias=aliases[0],
            )
        transferred.append(full_id)

    # Not converged yet: the next pass re-reads and confirms (or retries the
    # ones that errored into manual cleanup).
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"Transferred {len(transferred)}/{len(to_transfer)} bucket(s) to {new_key_id}",
        extras={
            "old_key_id": old_key_id,
            "new_key_id": new_key_id,
            "converged": False,
            "transferred": [b[:16] for b in transferred],
            "remaining": len(to_transfer) - len(transferred),
            "manual_cleanup_required": manual,
            "garage_stderr": "",
            "duration_seconds": _elapsed(started_at),
        },
    )


def _converged(
    old_key_id: str, new_key_id: str, *, transferred: list[str], started_at: float,
) -> JobOutcome:
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"Rotation {old_key_id} -> {new_key_id} converged",
        extras={
            "old_key_id": old_key_id,
            "new_key_id": new_key_id,
            "converged": True,
            "transferred": [b[:16] for b in transferred],
            "remaining": 0,
            "manual_cleanup_required": [],
            "garage_stderr": "",
            "duration_seconds": _elapsed(started_at),
        },
    )


def _failure(
    *,
    failure_reason: str,
    old_key_id: str,
    new_key_id: str,
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
            "old_key_id": old_key_id,
            "new_key_id": new_key_id,
            "converged": False,
            "remaining": -1,
            "manual_cleanup_required": [],
            "garage_stderr": stderr,
            "duration_seconds": _elapsed(started_at),
        },
    )


def _elapsed(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)
