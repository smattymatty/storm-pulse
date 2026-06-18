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

import json
import logging
import time
from typing import Any

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.garage import admin_api
from stormpulse.garage.config import GarageConfig

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

    # Leak path (BUCKETS-013): an explicit owned-bucket snapshot, captured
    # before the old key was reaped, fed in as JSON. When present, converge
    # from it instead of reading the (now-dead) old key.
    bucket_snapshot = None
    raw = params.get("bucket_snapshot")
    if raw:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            logger.error(
                "garage_converge_account_key_rotation: bad bucket_snapshot JSON",
            )
            return None
        if isinstance(parsed, list):
            bucket_snapshot = [
                e for e in parsed if isinstance(e, dict) and e.get("id")
            ]

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_converge_account_key_rotation(
            progress=progress,
            garage_config=garage_config,
            old_key_id=old_key_id,
            new_key_id=new_key_id,
            bucket_snapshot=bucket_snapshot,
        )

    return handler


def _owned(kinfo: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map ``full bucket id -> {"perms": (read, write, owner), "aliases": [...]}``
    for every bucket this key has any grant on.

    Per-tier, not owner-only (BUCKETS-014): a rotation must carry each grant at
    its actual tier so an rw/ro attach survives instead of silently dying. This
    also makes rotate strictly more correct, the new key inherits exactly what
    the old key could do, no more, no less.
    """
    owned: dict[str, dict[str, Any]] = {}
    for entry in kinfo.get("buckets") or []:
        full_id = entry.get("id") or ""
        perms = entry.get("permissions") or {}
        read = bool(perms.get("read"))
        write = bool(perms.get("write"))
        owner = bool(perms.get("owner"))
        if full_id and (read or write or owner):
            owned[full_id] = {
                "perms": (read, write, owner),
                "aliases": list(entry.get("localAliases") or []),
            }
    return owned


def _covers(
    new_entry: dict[str, Any] | None, old_perms: tuple[bool, bool, bool],
) -> bool:
    """True if the new key already holds every tier the old key had on a bucket
    (so nothing needs transferring for it)."""
    if new_entry is None:
        return False
    nr, nw, no = new_entry["perms"]
    o_r, o_w, o_o = old_perms
    return (nr or not o_r) and (nw or not o_w) and (no or not o_o)


async def run_converge_account_key_rotation(
    progress: ProgressCallback,
    garage_config: GarageConfig,
    old_key_id: str,
    new_key_id: str,
    bucket_snapshot: list[dict[str, str]] | None = None,
) -> JobOutcome:
    """Transfer ownership of the old key's buckets to the new key, once.

    ``bucket_snapshot`` (leak path) is the old key's owned buckets captured
    before it was reaped; when given, converge from it instead of reading the
    dead old key.
    """
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

    # ---- Step 1: determine the old key's owned buckets ----
    await progress("starting", 0, _TOTAL_STEPS, "Reading rotation state")
    if bucket_snapshot is not None:
        # Leak path: old key already reaped; use the captured snapshot. Each
        # entry may carry its tier (BUCKETS-014); default owner for snapshots
        # that predate per-tier capture.
        old_owned: dict[str, dict[str, Any]] = {}
        for e in bucket_snapshot:
            raw = e.get("perms")
            perms = (
                (bool(raw[0]), bool(raw[1]), bool(raw[2]))
                if isinstance(raw, (list, tuple)) and len(raw) == 3
                else (True, True, True)
            )
            old_owned[e["id"]] = {
                "perms": perms,
                "aliases": [e["alias"]] if e.get("alias") else [],
            }
    else:
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
        old_owned = _owned(old_info)

    new_info, new_err = admin_api.get_key_info(
        admin_url=admin_url, admin_token=admin_token, access_key_id=new_key_id,
    )
    if new_info is None:
        return _failure(
            failure_reason="new_key_read_failed",
            old_key_id=old_key_id, new_key_id=new_key_id,
            stderr=new_err, started_at=started_at,
        )

    new_owned = _owned(new_info)
    # A bucket needs a transfer until the new key covers every tier the old key
    # held on it (tier-aware, BUCKETS-014). Re-granting an existing grant is a
    # no-op, so passes stay idempotent.
    to_transfer = [
        bid for bid, v in old_owned.items()
        if not _covers(new_owned.get(bid), v["perms"])
    ]

    if not to_transfer:
        # A pass that finds nothing left to transfer is the converged signal.
        return _converged(old_key_id, new_key_id, transferred=[], started_at=started_at)

    # ---- Step 2: grant the new key the old key's tier + alias per bucket ----
    await progress("running", 1, _TOTAL_STEPS, f"Transferring {len(to_transfer)} bucket(s)")
    transferred: list[str] = []
    manual: list[dict[str, Any]] = []
    for full_id in to_transfer:
        read, write, owner = old_owned[full_id]["perms"]
        ok, err = admin_api.allow_bucket_key(
            admin_url=admin_url, admin_token=admin_token,
            bucket_ref=full_id, access_key_id=new_key_id,
            read=read, write=write, owner=owner,
        )
        if not ok:
            manual.append({"type": "grant", "bucket_id": full_id[:16], "error": err})
            continue
        aliases = old_owned[full_id]["aliases"]
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
