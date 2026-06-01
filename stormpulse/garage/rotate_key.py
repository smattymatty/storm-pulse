"""Handler for ``garage_rotate_customer_key``.

Four-step rotation: create new key, allow permissions, attach local alias,
delete old key. Permissions are per-(bucket, key) in Garage, so the new key
needs them granted explicitly - they can't be inherited.

Atomic rollback on any failure (rather than leaving the new key alive) is
forced by the dashboard data model: ``CustomerKey.garage_key_id`` is a
single field per ``(bucket, key_type)`` slot, so two live keys in one slot
is unrepresentable.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.config import GarageConfig
from stormpulse.garage.parse import GarageParseError, parse_key_create
from stormpulse.garage.runner import run_garage  # reuse subprocess helper

logger = logging.getLogger(__name__)


_TOTAL_STEPS = 4

_VALID_TIERS = frozenset({"all", "rw", "ro"})

_TIER_FLAGS: dict[str, list[str]] = {
    "all": ["--read", "--write", "--owner"],
    "rw": ["--read", "--write"],
    "ro": ["--read"],
}


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
    if key_tier not in _VALID_TIERS:
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
    perm_flags: list[str]
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
    if key_tier not in _VALID_TIERS:
        # Should be unreachable when called via the handler factory, but
        # keep a hard floor here so a direct caller can't slip past.
        raise ValueError(f"Invalid key_tier: {key_tier!r}")
    perm_flags = list(_TIER_FLAGS[key_tier])
    state = _RotateState(
        bucket_id=bucket_id,
        local_alias=local_alias,
        perm_flags=perm_flags,
    )

    # ---- Step 1: key create <new_key_name> ----
    await progress("starting", 0, _TOTAL_STEPS, "Creating new key")
    rc, stdout, stderr = await run_garage(
        garage_config,
        "key",
        "create",
        new_key_name,
    )
    if rc != 0:
        return _failure(
            failure_reason="new_key_create_failed",
            step_failed="new_key_create",
            state=state,
            stderr=stderr,
            started_at=started_at,
            rollback_status="not_required",
            extras_extra={},
        )
    try:
        key_result = parse_key_create(stdout)
    except GarageParseError as exc:
        # New key exists but we can't extract its ID. Operator must clean
        # it up by name; we know the name we asked for.
        return _failure(
            failure_reason="new_key_create_failed",
            step_failed="new_key_create",
            state=state,
            stderr=f"Could not parse key ID: {exc}",
            started_at=started_at,
            rollback_status="partial",
            extras_extra={
                "manual_cleanup_required": [
                    {"type": "key_unknown_id", "name": new_key_name},
                ],
            },
        )
    state.new_key_id = key_result.key_id
    state.new_key_secret = key_result.secret_key
    state.new_key_name = new_key_name
    state.step_completed = "new_key_create"

    # ---- Step 2: bucket allow <perm-flags> <bucket_id> --key <new_key_id> ----
    await progress("running", 1, _TOTAL_STEPS, "Granting permissions to new key")
    rc, _stdout, stderr = await run_garage(
        garage_config,
        "bucket",
        "allow",
        *perm_flags,
        bucket_id,
        "--key",
        state.new_key_id,
    )
    if rc != 0:
        rollback = await _rollback(garage_config, state)
        return _failure(
            failure_reason="new_key_permission_grant_failed",
            step_failed="new_key_permission_grant",
            state=state,
            stderr=stderr,
            started_at=started_at,
            rollback_status=rollback.status,
            extras_extra={"manual_cleanup_required": rollback.manual_cleanup},
        )
    state.new_key_permissions_granted = True
    state.step_completed = "new_key_permission_grant"

    # ---- Step 3: bucket alias --local <new_key_id> <bucket_id> <alias> ----
    await progress("running", 2, _TOTAL_STEPS, "Attaching local alias to new key")
    rc, _stdout, stderr = await run_garage(
        garage_config,
        "bucket",
        "alias",
        "--local",
        state.new_key_id,
        bucket_id,
        local_alias,
    )
    if rc != 0:
        rollback = await _rollback(garage_config, state)
        return _failure(
            failure_reason="new_key_alias_attach_failed",
            step_failed="new_key_alias_attach",
            state=state,
            stderr=stderr,
            started_at=started_at,
            rollback_status=rollback.status,
            extras_extra={"manual_cleanup_required": rollback.manual_cleanup},
        )
    state.new_alias_attached = True
    state.step_completed = "new_key_alias_attach"

    # ---- Step 4: key delete --yes <old_key_id> ----
    await progress("running", 3, _TOTAL_STEPS, "Deleting old key")
    rc, _stdout, stderr = await run_garage(
        garage_config,
        "key",
        "delete",
        "--yes",
        old_key_id,
    )
    if rc != 0:
        rollback = await _rollback(garage_config, state)
        return _failure(
            failure_reason="old_key_delete_failed",
            step_failed="old_key_delete",
            state=state,
            stderr=stderr,
            started_at=started_at,
            rollback_status=rollback.status,
            extras_extra={"manual_cleanup_required": rollback.manual_cleanup},
        )
    state.step_completed = "old_key_delete"

    # ---- Success ----
    await progress("finalizing", _TOTAL_STEPS, _TOTAL_STEPS, "Rotation complete")
    assert state.new_key_id is not None
    assert state.new_key_secret is not None
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=(f"New key ID: {state.new_key_id}\nOld key {old_key_id} deleted."),
        extras={
            "new_key_id": state.new_key_id,
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

    # 1. Detach new key's local alias if attached
    if state.new_alias_attached and state.new_key_id is not None:
        try:
            rc, _stdout, _stderr = await run_garage(
                garage_config,
                "bucket",
                "unalias",
                "--local",
                state.new_key_id,
                state.local_alias,
            )
        except (TimeoutError, OSError) as exc:
            logger.warning(
                "Rollback: unalias --local %s failed: %s",
                state.new_key_id,
                exc,
            )
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
        if rc != 0:
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
        try:
            rc, _stdout, _stderr = await run_garage(
                garage_config,
                "bucket",
                "deny",
                *state.perm_flags,
                state.bucket_id,
                "--key",
                state.new_key_id,
            )
        except (TimeoutError, OSError) as exc:
            logger.warning(
                "Rollback: bucket deny on %s failed: %s",
                state.new_key_id,
                exc,
            )
            manual.append(
                {
                    "type": "permission_grant",
                    "key_id": state.new_key_id,
                    "bucket_id": state.bucket_id,
                }
            )
            manual.append({"type": "key", "id": state.new_key_id})
            return _RollbackResult(status="partial", manual_cleanup=manual)
        if rc != 0:
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
        try:
            rc, _stdout, _stderr = await run_garage(
                garage_config,
                "key",
                "delete",
                "--yes",
                state.new_key_id,
            )
        except (TimeoutError, OSError) as exc:
            logger.warning(
                "Rollback: key delete %s failed: %s",
                state.new_key_id,
                exc,
            )
            manual.append({"type": "key", "id": state.new_key_id})
            return _RollbackResult(status="partial", manual_cleanup=manual)
        if rc != 0:
            manual.append({"type": "key", "id": state.new_key_id})
            return _RollbackResult(status="partial", manual_cleanup=manual)

    return _RollbackResult(status="complete", manual_cleanup=[])


def _failure(
    *,
    failure_reason: str,
    step_failed: str,
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
