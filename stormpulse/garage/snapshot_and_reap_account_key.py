"""Handler for ``garage_snapshot_and_reap_account_key``.

Leak-rotate steps 1+2 (BUCKETS-013): snapshot the old key's owned buckets,
then delete the key object outright. Order is the whole point:

  1. **Snapshot first** (GetKeyInfo) - capture the owned-bucket list BEFORE
     anything destructive, so the list can never be lost when the key dies.
  2. **Reap the object now** - DeleteKey, not a per-bucket deny. A live
     compromised key keeps ``allow_create_bucket`` and could keep spawning
     buckets through the whole convergence; deleting the object is the fastest,
     most complete kill. It dies on the first successful admin-API delete.

The new key inherits ownership afterward via converge-from-snapshot (Storm
feeds this snapshot into ``garage_converge_account_key_rotation``). If the old
key is already gone (404), the snapshot is empty and the reap is a no-op
success (idempotent).
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


def make_snapshot_and_reap_account_key_handler(
    garage_config: GarageConfig,
    params: dict[str, str],
) -> JobHandler | None:
    """Build a JobHandler from runtime params. Required: ``old_key_id``."""
    if not params.get("old_key_id"):
        logger.error("garage_snapshot_and_reap_account_key missing old_key_id")
        return None
    old_key_id = params["old_key_id"]

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_snapshot_and_reap_account_key(
            progress=progress, garage_config=garage_config, old_key_id=old_key_id,
        )

    return handler


def _owned_snapshot(kinfo: dict[str, Any]) -> list[dict[str, Any]]:
    """``[{"id", "alias", "perms": [read, write, owner]}]`` for every bucket the
    key has any grant on.

    Per-tier (BUCKETS-014): a leak-rotate must carry the compromised key's
    rw/ro attaches to the replacement, not only the buckets it owns, so the
    snapshot captures every grant and its tier (the convergence pass grants at
    that tier).
    """
    snapshot: list[dict[str, Any]] = []
    for entry in kinfo.get("buckets") or []:
        full_id = entry.get("id") or ""
        perms = entry.get("permissions") or {}
        read = bool(perms.get("read"))
        write = bool(perms.get("write"))
        owner = bool(perms.get("owner"))
        if not (full_id and (read or write or owner)):
            continue
        aliases = entry.get("localAliases") or []
        snapshot.append({
            "id": full_id,
            "alias": aliases[0] if aliases else "",
            "perms": [read, write, owner],
        })
    return snapshot


async def run_snapshot_and_reap_account_key(
    progress: ProgressCallback,
    garage_config: GarageConfig,
    old_key_id: str,
) -> JobOutcome:
    """Snapshot the key's owned buckets, then delete the key object."""
    started_at = time.monotonic()
    admin_url, admin_token = garage_config.admin_url, garage_config.admin_token
    if not (admin_url and admin_token):
        return _failure(
            failure_reason="admin_api_unconfigured",
            old_key_id=old_key_id,
            stderr=(
                "Garage admin API not configured (admin_url + admin_token); set "
                "[garage] admin_url and admin_token_file."
            ),
            started_at=started_at,
        )

    # ---- Step 1: snapshot (before anything destructive) ----
    await progress("starting", 0, _TOTAL_STEPS, "Snapshotting owned buckets")
    kinfo, kerr = admin_api.get_key_info(
        admin_url=admin_url, admin_token=admin_token, access_key_id=old_key_id,
    )
    if kinfo is None:
        if admin_api.is_not_found(kerr):
            # Already gone: empty snapshot, nothing to reap. Fallback to
            # manual re-claim is the customer's path (no snapshot to converge).
            return _success(old_key_id, snapshot=[], started_at=started_at)
        return _failure(
            failure_reason="snapshot_read_failed",
            old_key_id=old_key_id, stderr=kerr, started_at=started_at,
        )
    snapshot = _owned_snapshot(kinfo)

    # ---- Step 2: reap the key object now ----
    await progress("running", 1, _TOTAL_STEPS, "Deleting compromised key")
    ok, err = admin_api.delete_key(
        admin_url=admin_url, admin_token=admin_token, access_key_id=old_key_id,
    )
    if not (ok or admin_api.is_not_found(err)):
        # The kill did not land: do NOT certify gone. Storm retries the whole
        # snapshot-and-reap (re-snapshot is idempotent).
        return _failure(
            failure_reason="reap_failed",
            old_key_id=old_key_id, stderr=err, started_at=started_at,
        )

    return _success(old_key_id, snapshot=snapshot, started_at=started_at)


def _success(
    old_key_id: str, *, snapshot: list[dict[str, str]], started_at: float,
) -> JobOutcome:
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"Snapshotted {len(snapshot)} bucket(s) and reaped {old_key_id}",
        extras={
            "old_key_id": old_key_id,
            "reaped": True,
            "snapshot": snapshot,
            "garage_stderr": "",
            "duration_seconds": _elapsed(started_at),
        },
    )


def _failure(
    *, failure_reason: str, old_key_id: str, stderr: str, started_at: float,
) -> JobOutcome:
    return JobOutcome(
        success=False,
        exit_code=-1,
        stdout="",
        stderr=stderr,
        failure_reason=failure_reason,
        extras={
            "old_key_id": old_key_id,
            "reaped": False,
            "snapshot": [],
            "garage_stderr": stderr,
            "duration_seconds": _elapsed(started_at),
        },
    )


def _elapsed(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)
