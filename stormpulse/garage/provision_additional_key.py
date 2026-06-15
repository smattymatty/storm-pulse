"""Handler for ``garage_provision_additional_key``.

Adds a new tiered (rw or ro) key to an existing bucket: create key, allow
permissions, attach local alias. Atomic rollback runs in reverse order; the
bucket is never touched by rollback (this handler owns the new key only).

All Garage interaction is the admin HTTP API (ADR garage/001), never the CLI.
The bucket reference is the 16-char UUID prefix stored in
``CustomerBucket.garage_bucket_id``; the admin client resolves it to Garage's
full 64-char id before each bucket-scoped call.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.config import GarageConfig
from stormpulse.garage import admin_api

logger = logging.getLogger(__name__)


_TOTAL_STEPS = 3

_VALID_TIERS = ("all", "rw", "ro")

# (read, write, owner) for each tier. The ``all`` tier mints an owner key onto
# a bucket whose owner slot is free - the claim-admin path for an adopted
# bucket (BUCKETS-013), the first owner-grant-on-existing-bucket in the system.
# rw/ro remain non-owner tiered keys added to a bucket that already has one.
_TIER_PERMS: dict[str, tuple[bool, bool, bool]] = {
    "all": (True, True, True),
    "rw": (True, True, False),
    "ro": (True, False, False),
}


def make_provision_additional_key_handler(
    garage_config: GarageConfig,
    params: dict[str, str],
) -> JobHandler | None:
    """Build a JobHandler from runtime params.

    Required params: ``new_key_name``, ``bucket_id``, ``local_alias``,
    ``key_tier`` (one of ``rw``, ``ro``). Returns ``None`` if any are
    missing or ``key_tier`` is invalid.
    """
    required = ("new_key_name", "bucket_id", "local_alias", "key_tier")
    if not all(params.get(k) for k in required):
        logger.error(
            "garage_provision_additional_key missing required params: %s",
            [k for k in required if not params.get(k)],
        )
        return None
    if params["key_tier"] not in _VALID_TIERS:
        logger.error(
            "garage_provision_additional_key invalid key_tier: %s",
            params["key_tier"],
        )
        return None

    new_key_name = params["new_key_name"]
    bucket_id = params["bucket_id"]
    local_alias = params["local_alias"]
    key_tier = params["key_tier"]

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_provision_additional_key(
            progress=progress,
            garage_config=garage_config,
            new_key_name=new_key_name,
            bucket_id=bucket_id,
            local_alias=local_alias,
            key_tier=key_tier,
        )

    return handler


@dataclass
class _AdditionalKeyState:
    """What's been created so far, for rollback."""

    bucket_id: str
    local_alias: str
    key_tier: str
    new_key_id: str | None = None
    perms_granted: bool = False
    step_completed: str | None = None


async def run_provision_additional_key(
    progress: ProgressCallback,
    garage_config: GarageConfig,
    new_key_name: str,
    bucket_id: str,
    local_alias: str,
    key_tier: str,
) -> JobOutcome:
    """Create a new tiered key, grant it permissions on the bucket, and
    attach a local alias. Three steps with atomic rollback.
    """
    started_at = time.monotonic()
    state = _AdditionalKeyState(
        bucket_id=bucket_id,
        local_alias=local_alias,
        key_tier=key_tier,
    )

    if key_tier not in _VALID_TIERS:
        # Defensive: handler factory rejects this, but enforce here too.
        return _failure(
            failure_reason="invalid_key_tier",
            step_failed=None,
            state=state,
            stderr=f"key_tier must be one of {_VALID_TIERS}, got {key_tier!r}",
            started_at=started_at,
            rollback_status="not_required",
            extras_extra={},
        )

    admin_url, admin_token = garage_config.admin_url, garage_config.admin_token
    if not (admin_url and admin_token):
        # Fail loud: a migrated operation never silently no-ops (ADR garage/001).
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

    read, write, owner = _TIER_PERMS[key_tier]
    new_secret: str | None = None

    # ---- Step 1: CreateKey ----
    await progress("starting", 0, _TOTAL_STEPS, "Creating new key")
    info, err = admin_api.create_key(
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
    new_secret = info.get("secretAccessKey") or ""
    if not new_key_id:
        # The key was created but the response didn't identify it; we can't
        # roll it back without its id, so flag it for manual cleanup.
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
    state.step_completed = "new_key_create"

    # ---- Step 2: AllowBucketKey ----
    await progress("running", 1, _TOTAL_STEPS, f"Granting {key_tier} permissions")
    ok, err = admin_api.allow_bucket_key(
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
    state.perms_granted = True
    state.step_completed = "new_key_permission_grant"

    # ---- Step 3: AddBucketAlias (local variant) ----
    await progress("running", 2, _TOTAL_STEPS, "Attaching local alias")
    ok, err = admin_api.add_bucket_alias_local(
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
    state.step_completed = "new_key_alias_attach"

    # ---- Success ----
    await progress("finalizing", _TOTAL_STEPS, _TOTAL_STEPS, "Provisioning complete")
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"New {key_tier} key: {new_key_id}",
        extras={
            "new_key_id": new_key_id,
            "new_secret": new_secret,
            "new_key_name": new_key_name,
            "key_tier": key_tier,
            "step_completed": state.step_completed,
            "step_failed": None,
            "rollback_status": "not_required",
            "manual_cleanup_required": [],
            "garage_stderr": "",
            "duration_seconds": _elapsed(started_at),
        },
    )


@dataclass(frozen=True)
class _RollbackResult:
    status: str  # "complete" | "partial"
    manual_cleanup: list[dict[str, Any]]


async def _rollback(
    garage_config: GarageConfig,
    state: _AdditionalKeyState,
) -> _RollbackResult:
    """Reverse-order cleanup. Halt on first failure.

    Order:
      1. Revoke permissions if granted.
      2. Delete the new key if created.

    The bucket is never touched - this orchestrator does not own it.
    There is no alias-detach phase because step 3 (alias attach) is
    the final forward step; if it fails the alias was never attached,
    and if it succeeds there's nothing to roll back from.
    """
    manual: list[dict[str, Any]] = []
    admin_url, admin_token = garage_config.admin_url, garage_config.admin_token
    read, write, owner = _TIER_PERMS[state.key_tier]

    # 1. Revoke permissions
    if state.perms_granted and state.new_key_id is not None:
        ok, _err = admin_api.deny_bucket_key(
            admin_url=admin_url,
            admin_token=admin_token,
            bucket_ref=state.bucket_id,
            access_key_id=state.new_key_id,
            read=read,
            write=write,
            owner=owner,
        )
        if not ok:
            manual.extend(_remaining_after_perm_halt(state, read, write, owner))
            return _RollbackResult(status="partial", manual_cleanup=manual)

    # 2. Delete new key
    if state.new_key_id is not None:
        ok, _err = admin_api.delete_key(
            admin_url=admin_url,
            admin_token=admin_token,
            access_key_id=state.new_key_id,
        )
        if not ok:
            manual.append({"type": "key", "id": state.new_key_id})
            return _RollbackResult(status="partial", manual_cleanup=manual)

    return _RollbackResult(status="complete", manual_cleanup=[])


def _remaining_after_perm_halt(
    state: _AdditionalKeyState,
    read: bool,
    write: bool,
    owner: bool,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if state.new_key_id is not None:
        if state.perms_granted:
            items.append(
                {
                    "type": "permission_grant",
                    "key_id": state.new_key_id,
                    "permissions": {"read": read, "write": write, "owner": owner},
                }
            )
        items.append({"type": "key", "id": state.new_key_id})
    return items


def _failure(
    *,
    failure_reason: str,
    step_failed: str | None,
    state: _AdditionalKeyState,
    stderr: str,
    started_at: float,
    rollback_status: str,
    extras_extra: dict[str, Any],
) -> JobOutcome:
    extras: dict[str, Any] = {
        "new_key_id": state.new_key_id,
        "key_tier": state.key_tier,
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
