"""Handler for the ``garage_provision_additional_key`` command.

Adds a new tiered key (rw or ro) to an existing bucket. The bucket
must already exist with at least one alias (typically the admin key's
local alias from ``provision_customer_bucket``).

Step ordering:

  1. key create <new_key_name>
  2. bucket allow <tier-flags> <bucket_id> --key <new_key_id>
  3. bucket alias --local <new_key_id> <bucket_id> <local_alias>

On failure, atomic rollback runs in reverse order (revoke perms,
delete key). The bucket is never touched by rollback — this
orchestrator does not own the bucket, only the new key.

The bucket reference is the 16-char UUID prefix that Storm stores in
``CustomerBucket.garage_bucket_id``. Garage CLI accepts that as a
bucket reference; the full 64-char form is rejected.

This is the ``rotate_key`` shape minus the old-key-deletion step:
identical scaffolding, simpler completion contract.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.config import GarageConfig
from stormpulse.garage.parse import GarageParseError, parse_key_create
from stormpulse.garage.provision_bucket import _run_garage  # reuse helper

logger = logging.getLogger(__name__)


_TOTAL_STEPS = 3

_VALID_TIERS = ("rw", "ro")

_TIER_FLAGS: dict[str, tuple[str, ...]] = {
    "rw": ("--read", "--write"),
    "ro": ("--read",),
}


# ---------------------------------------------------------------------------
# Public entrypoint — called by agent.py
# ---------------------------------------------------------------------------


def make_provision_additional_key_handler(
    garage_config: GarageConfig, params: dict[str, str],
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


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------


@dataclass
class _AdditionalKeyState:
    """What's been created so far, for rollback."""

    bucket_id: str
    local_alias: str
    key_tier: str
    new_key_id: str | None = None
    perms_granted: bool = False
    step_completed: str | None = None


# ---------------------------------------------------------------------------
# Core orchestration
# ---------------------------------------------------------------------------


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
    if key_tier not in _VALID_TIERS:
        # Defensive: handler factory rejects this, but enforce here too.
        return _failure(
            failure_reason="invalid_key_tier",
            step_failed=None,
            state=_AdditionalKeyState(
                bucket_id=bucket_id,
                local_alias=local_alias,
                key_tier=key_tier,
            ),
            stderr=f"key_tier must be one of {_VALID_TIERS}, got {key_tier!r}",
            started_at=started_at,
            rollback_status="not_required",
            extras_extra={},
        )

    flags = _TIER_FLAGS[key_tier]
    state = _AdditionalKeyState(
        bucket_id=bucket_id,
        local_alias=local_alias,
        key_tier=key_tier,
    )
    new_secret: str | None = None

    # ---- Step 1: key create <new_key_name> ----
    await progress("starting", 0, _TOTAL_STEPS, "Creating new key")
    rc, stdout, stderr = await _run_garage(
        garage_config, "key", "create", new_key_name,
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
    new_secret = key_result.secret_key
    state.step_completed = "new_key_create"

    # ---- Step 2: bucket allow <flags> <bucket_id> --key <new_key_id> ----
    await progress(
        "running", 1, _TOTAL_STEPS,
        f"Granting {key_tier} permissions",
    )
    rc, _stdout, stderr = await _run_garage(
        garage_config,
        "bucket", "allow", *flags,
        bucket_id, "--key", state.new_key_id,
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
            extras_extra={
                "manual_cleanup_required": rollback.manual_cleanup,
            },
        )
    state.perms_granted = True
    state.step_completed = "new_key_permission_grant"

    # ---- Step 3: bucket alias --local <new_key> <bucket_id> <local_alias> ----
    await progress(
        "running", 2, _TOTAL_STEPS, "Attaching local alias",
    )
    rc, _stdout, stderr = await _run_garage(
        garage_config,
        "bucket", "alias", "--local", state.new_key_id,
        bucket_id, local_alias,
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
            extras_extra={
                "manual_cleanup_required": rollback.manual_cleanup,
            },
        )
    state.step_completed = "new_key_alias_attach"

    # ---- Success ----
    await progress("finalizing", _TOTAL_STEPS, _TOTAL_STEPS, "Provisioning complete")
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"New {key_tier} key: {state.new_key_id}",
        extras={
            "new_key_id": state.new_key_id,
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


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RollbackResult:
    status: str  # "complete" | "partial"
    manual_cleanup: list[dict[str, Any]]


async def _rollback(
    garage_config: GarageConfig, state: _AdditionalKeyState,
) -> _RollbackResult:
    """Reverse-order cleanup. Halt on first failure.

    Order:
      1. Revoke permissions if granted.
      2. Delete the new key if created.

    The bucket is never touched — this orchestrator does not own it.
    There is no alias-detach phase because step 3 (alias attach) is
    the final forward step; if it fails the alias was never attached,
    and if it succeeds there's nothing to roll back from.
    """
    manual: list[dict[str, Any]] = []
    flags = _TIER_FLAGS[state.key_tier]

    # 1. Revoke permissions
    if state.perms_granted and state.new_key_id is not None:
        try:
            rc, _stdout, _stderr = await _run_garage(
                garage_config,
                "bucket", "deny", *flags,
                state.bucket_id, "--key", state.new_key_id,
            )
        except (asyncio.TimeoutError, OSError) as exc:
            logger.warning(
                "Rollback: bucket deny %s failed: %s",
                state.new_key_id, exc,
            )
            manual.extend(_remaining_after_perm_halt(state, flags))
            return _RollbackResult(status="partial", manual_cleanup=manual)
        if rc != 0:
            manual.extend(_remaining_after_perm_halt(state, flags))
            return _RollbackResult(status="partial", manual_cleanup=manual)

    # 3. Delete new key
    if state.new_key_id is not None:
        try:
            rc, _stdout, _stderr = await _run_garage(
                garage_config, "key", "delete", "--yes", state.new_key_id,
            )
        except (asyncio.TimeoutError, OSError) as exc:
            logger.warning(
                "Rollback: key delete %s failed: %s",
                state.new_key_id, exc,
            )
            manual.append({"type": "key", "id": state.new_key_id})
            return _RollbackResult(status="partial", manual_cleanup=manual)
        if rc != 0:
            manual.append({"type": "key", "id": state.new_key_id})
            return _RollbackResult(status="partial", manual_cleanup=manual)

    return _RollbackResult(status="complete", manual_cleanup=[])


def _remaining_after_perm_halt(
    state: _AdditionalKeyState, flags: tuple[str, ...],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if state.new_key_id is not None:
        if state.perms_granted:
            items.append({
                "type": "permission_grant",
                "key_id": state.new_key_id,
                "flags": list(flags),
            })
        items.append({"type": "key", "id": state.new_key_id})
    return items


# ---------------------------------------------------------------------------
# Result construction
# ---------------------------------------------------------------------------


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
    final_reason = (
        "rollback_failed" if rollback_status == "partial" else failure_reason
    )
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
