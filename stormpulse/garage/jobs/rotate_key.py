"""Handler for ``garage_rotate_customer_key``.

Four-step rotation: create new key, allow permissions, attach local alias,
delete old key. Permissions are per-(bucket, key) in Garage, so the new key
needs them granted explicitly - they can't be inherited.

Atomic rollback on any failure (rather than leaving the new key alive) is
forced by the dashboard data model: ``CustomerKey.garage_key_id`` is a
single field per ``(bucket, key_type)`` slot, so two live keys in one slot
is unrepresentable.

All Garage interaction is the admin HTTP API, never the CLI.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.garage import admin_api
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.tiers import TIER_PERMS

logger = logging.getLogger(__name__)


_TOTAL_STEPS = 4



def make_rotate_customer_key_handler(
    garage_config: GarageConfig,
    params: dict[str, str],
) -> JobHandler | None:
    """Build a JobHandler from runtime params, or ``None`` if invalid."""
    required = (
        "old_key_id",
        "new_key_name",
        "bucket_id",
        "local_alias",
        "key_tier",
    )
    if not all(params.get(k) for k in required):
        logger.error(
            "garage_rotate_customer_key missing required params: %s",
            [k for k in required if not params.get(k)],
        )
        return None

    old_key_id = params["old_key_id"]
    new_key_name = params["new_key_name"]
    bucket_id = params["bucket_id"]
    local_alias = params["local_alias"]
    key_tier = params["key_tier"]

    # Defence in depth: don't trust the dispatcher's regex to be the
    # only check. An unknown tier here is treated as a missing param.
    if key_tier not in TIER_PERMS:
        logger.error(
            "garage_rotate_customer_key got invalid key_tier: %r",
            key_tier,
        )
        return None

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_rotate_customer_key(
            progress=progress,
            garage_config=garage_config,
            old_key_id=old_key_id,
            new_key_name=new_key_name,
            bucket_id=bucket_id,
            local_alias=local_alias,
            key_tier=key_tier,
        )

    return handler


@dataclass
class _RotateState:
    bucket_id: str
    local_alias: str
    read: bool
    write: bool
    owner: bool
    new_key_id: str | None = None
    new_key_secret: str | None = None
    new_key_name: str | None = None
    new_key_permissions_granted: bool = False
    new_alias_attached: bool = False
    step_completed: str | None = None


@dataclass(frozen=True)
class _RollbackResult:
    status: str  # "complete" | "partial"
    manual_cleanup: list[dict[str, str]]


async def run_rotate_customer_key(
    progress: ProgressCallback,
    garage_config: GarageConfig,
    old_key_id: str,
    new_key_name: str,
    bucket_id: str,
    local_alias: str,
    key_tier: str,
) -> JobOutcome:
    started_at = time.monotonic()
    if key_tier not in TIER_PERMS:
        # Should be unreachable when called via the handler factory, but
        # keep a hard floor here so a direct caller can't slip past.
        raise ValueError(f"Invalid key_tier: {key_tier!r}")
    read, write, owner = TIER_PERMS[key_tier]
    state = _RotateState(
        bucket_id=bucket_id,
        local_alias=local_alias,
        read=read,
        write=write,
        owner=owner,
    )

    admin_url, admin_token = garage_config.admin_url, garage_config.admin_token
    if not (admin_url and admin_token):
        # Fail loud: a migrated operation never silently no-ops.
        return _failure(
            failure_reason="admin_api_unconfigured",
            step_failed=None,
            state=state,
            stderr=(
                "Garage admin API not configured (admin_url + admin_token); set "
                "[garage] admin_url and admin_token_file."
            ),
            started_at=started_at,
            rollback_status="not_required",
            extras_extra={},
        )

    # ---- Step 1: CreateKey ----
    await progress("starting", 0, _TOTAL_STEPS, "Creating new key")
    info, err = await asyncio.to_thread(
        admin_api.create_key,
        admin_url=admin_url, admin_token=admin_token, name=new_key_name,
    )
    if info is None:
        return _failure(
            failure_reason="new_key_create_failed",
            step_failed="new_key_create",
            state=state,
            stderr=err,
            started_at=started_at,
            rollback_status="not_required",
            extras_extra={},
        )
    new_key_id = info.get("accessKeyId") or ""
    if not new_key_id:
        # New key exists but the response didn't identify it; operator must
        # clean it up by name, which is the name we asked for.
        return _failure(
            failure_reason="new_key_create_failed",
            step_failed="new_key_create",
            state=state,
            stderr="CreateKey response missing accessKeyId",
            started_at=started_at,
            rollback_status="partial",
            extras_extra={
                "manual_cleanup_required": [
                    {"type": "key_unknown_id", "name": new_key_name},
                ],
            },
        )
    state.new_key_id = new_key_id
    state.new_key_secret = info.get("secretAccessKey") or ""
    state.new_key_name = new_key_name
    state.step_completed = "new_key_create"

    # ---- Step 2: AllowBucketKey ----
    await progress("running", 1, _TOTAL_STEPS, "Granting permissions to new key")
    ok, err = await asyncio.to_thread(
        admin_api.allow_bucket_key,
        admin_url=admin_url,
        admin_token=admin_token,
        bucket_ref=bucket_id,
        access_key_id=new_key_id,
        read=read,
        write=write,
        owner=owner,
    )
    if not ok:
        rollback = await _rollback(garage_config, state)
        return _failure(
            failure_reason="new_key_permission_grant_failed",
            step_failed="new_key_permission_grant",
            state=state,
            stderr=err,
            started_at=started_at,
            rollback_status=rollback.status,
            extras_extra={"manual_cleanup_required": rollback.manual_cleanup},
        )
    state.new_key_permissions_granted = True
    state.step_completed = "new_key_permission_grant"

    # ---- Step 3: AddBucketAlias (local) on the new key ----
    await progress("running", 2, _TOTAL_STEPS, "Attaching local alias to new key")
    ok, err = await asyncio.to_thread(
        admin_api.add_bucket_alias_local,
        admin_url=admin_url,
        admin_token=admin_token,
        bucket_ref=bucket_id,
        access_key_id=new_key_id,
        local_alias=local_alias,
    )
    if not ok:
        rollback = await _rollback(garage_config, state)
        return _failure(
            failure_reason="new_key_alias_attach_failed",
            step_failed="new_key_alias_attach",
            state=state,
            stderr=err,
            started_at=started_at,
            rollback_status=rollback.status,
            extras_extra={"manual_cleanup_required": rollback.manual_cleanup},
        )
    state.new_alias_attached = True
    state.step_completed = "new_key_alias_attach"

    # ---- Step 4: DeleteKey (the old key) ----
    await progress("running", 3, _TOTAL_STEPS, "Deleting old key")
    ok, err = await asyncio.to_thread(
        admin_api.delete_key,
        admin_url=admin_url, admin_token=admin_token, access_key_id=old_key_id,
    )
    if not ok:
        rollback = await _rollback(garage_config, state)
        return _failure(
            failure_reason="old_key_delete_failed",
            step_failed="old_key_delete",
            state=state,
            stderr=err,
            started_at=started_at,
            rollback_status=rollback.status,
            extras_extra={"manual_cleanup_required": rollback.manual_cleanup},
        )
    state.step_completed = "old_key_delete"

    # ---- Success ----
    await progress("finalizing", _TOTAL_STEPS, _TOTAL_STEPS, "Rotation complete")
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=(f"New key ID: {new_key_id}\nOld key {old_key_id} deleted."),
        extras={
            "new_key_id": new_key_id,
            "new_secret": state.new_key_secret,
            "new_key_name": state.new_key_name,
            "step_completed": state.step_completed,
            "step_failed": None,
            "rollback_status": "not_required",
            "manual_cleanup_required": [],
            "garage_stderr": "",
            "duration_seconds": _elapsed(started_at),
        },
    )


async def _rollback(
    garage_config: GarageConfig,
    state: _RotateState,
) -> _RollbackResult:
    """Reverse-order cleanup: detach alias, revoke perms, delete new key.

    Old key is never touched by rollback - the whole point of full
    rollback is leaving the old key in its pre-call state.
    """
    manual: list[dict[str, str]] = []
    admin_url, admin_token = garage_config.admin_url, garage_config.admin_token

    # 1. Detach new key's local alias if attached
    if state.new_alias_attached and state.new_key_id is not None:
        ok, _err = await asyncio.to_thread(
            admin_api.remove_bucket_alias_local,
            admin_url=admin_url,
            admin_token=admin_token,
            bucket_ref=state.bucket_id,
            access_key_id=state.new_key_id,
            local_alias=state.local_alias,
        )
        if not ok:
            manual.append(
                {
                    "type": "local_alias",
                    "key_id": state.new_key_id,
                    "alias": state.local_alias,
                }
            )
            if state.new_key_permissions_granted:
                manual.append(
                    {
                        "type": "permission_grant",
                        "key_id": state.new_key_id,
                        "bucket_id": state.bucket_id,
                    }
                )
            manual.append({"type": "key", "id": state.new_key_id})
            return _RollbackResult(status="partial", manual_cleanup=manual)

    # 2. Revoke permissions on the new key if granted
    if state.new_key_permissions_granted and state.new_key_id is not None:
        ok, _err = await asyncio.to_thread(
            admin_api.deny_bucket_key,
            admin_url=admin_url,
            admin_token=admin_token,
            bucket_ref=state.bucket_id,
            access_key_id=state.new_key_id,
            read=state.read,
            write=state.write,
            owner=state.owner,
        )
        if not ok:
            manual.append(
                {
                    "type": "permission_grant",
                    "key_id": state.new_key_id,
                    "bucket_id": state.bucket_id,
                }
            )
            manual.append({"type": "key", "id": state.new_key_id})
            return _RollbackResult(status="partial", manual_cleanup=manual)

    # 3. Delete new key
    if state.new_key_id is not None:
        ok, _err = await asyncio.to_thread(
            admin_api.delete_key,
            admin_url=admin_url,
            admin_token=admin_token,
            access_key_id=state.new_key_id,
        )
        if not ok:
            manual.append({"type": "key", "id": state.new_key_id})
            return _RollbackResult(status="partial", manual_cleanup=manual)

    return _RollbackResult(status="complete", manual_cleanup=[])


def _failure(
    *,
    failure_reason: str,
    step_failed: str | None,
    state: _RotateState,
    stderr: str,
    started_at: float,
    rollback_status: str,
    extras_extra: dict[str, Any],
) -> JobOutcome:
    extras: dict[str, Any] = {
        "new_key_id": state.new_key_id,
        "step_completed": state.step_completed,
        "step_failed": step_failed,
        "rollback_status": rollback_status,
        "manual_cleanup_required": [],
        "garage_stderr": stderr,
        "duration_seconds": _elapsed(started_at),
    }
    extras.update(extras_extra)
    final_reason = "rollback_failed" if rollback_status == "partial" else failure_reason
    return JobOutcome(
        success=False,
        exit_code=-1,
        stdout="",
        stderr=stderr,
        failure_reason=final_reason,
        extras=extras,
    )


def _elapsed(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)
