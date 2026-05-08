"""Handler for the ``garage_provision_customer_bucket`` long-running command.

Creates a bucket and its admin key atomically. Additional keys (rw/ro)
are added later via ``provision_additional_key``.

Step ordering:

  1. bucket create <throwaway>
  2. key create <admin name>
  3. bucket allow --read --write --owner <throwaway> --key <admin id>
  4. bucket alias --local <admin id> <throwaway> <display>
  5. bucket unalias <throwaway>

The throwaway alias is the bucket's only globally-unique handle
during steps 1-4. Step 5 removes it; the admin's local alias
satisfies Garage's "must have at least one alias" orphan rule.

On any failure, atomic rollback runs in reverse order: detach local
alias, revoke permissions, delete key, delete bucket. The throwaway
is always still attached during rollback (it's removed only by the
final step in the success path), so all rollback CLI calls reference
the bucket by ``throwaway_alias``.

The contract — step ordering, rollback table, failure-reason
vocabulary, and idempotency rule — is documented in
``_architecture/specs/cellar-bucket-naming-foundation.md`` (Issue 4).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.config import GarageConfig
from stormpulse.garage.parse import (
    GarageParseError,
    parse_bucket_info,
    parse_key_create,
)

logger = logging.getLogger(__name__)


# Per-step subprocess timeout. Garage CLI calls are typically <1s; the
# generous bound covers cluster-load spikes without letting a hung call
# block the whole job indefinitely.
_STEP_TIMEOUT_SECONDS = 30

_TOTAL_STEPS = 5

_ADMIN_FLAGS = ("--read", "--write", "--owner")


# ---------------------------------------------------------------------------
# Public entrypoint — called by agent.py
# ---------------------------------------------------------------------------


def make_provision_customer_bucket_handler(
    garage_config: GarageConfig, params: dict[str, str],
) -> JobHandler | None:
    """Build a JobHandler from runtime params.

    Returns ``None`` if a required param is missing — the caller emits a
    structured no-handler failure rather than crashing.
    """
    required = ("display_name", "key_name_admin")
    if not all(params.get(k) for k in required):
        logger.error(
            "garage_provision_customer_bucket missing required params: %s",
            [k for k in required if not params.get(k)],
        )
        return None

    display_name = params["display_name"]
    key_name_admin = params["key_name_admin"]

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_provision_customer_bucket(
            progress=progress,
            garage_config=garage_config,
            display_name=display_name,
            key_name_admin=key_name_admin,
        )

    return handler


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------


@dataclass
class _ProvisionState:
    """What's been created so far, for rollback purposes."""

    display_name: str
    throwaway_alias: str
    bucket_uuid: str | None = None
    throwaway_attached: bool = False
    admin_key_id: str | None = None
    admin_perms_granted: bool = False
    admin_alias_attached: bool = False
    step_completed: str | None = None


@dataclass(frozen=True)
class _AdminKey:
    """Captured admin key info from step 2."""

    key_id: str
    secret_key: str
    key_name: str


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------


async def _run_garage(
    garage_config: GarageConfig, *args: str, timeout: float = _STEP_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    """Run ``docker exec <container> /garage <args>``.

    Returns ``(returncode, stdout, stderr)``. On timeout, the subprocess is
    killed and ``TimeoutError`` propagates.
    """
    cmd = [
        garage_config.docker_binary,
        "exec",
        garage_config.container_name,
        garage_config.garage_binary,
        *args,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return (
        proc.returncode if proc.returncode is not None else -1,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


# ---------------------------------------------------------------------------
# Core orchestration
# ---------------------------------------------------------------------------


async def run_provision_customer_bucket(
    progress: ProgressCallback,
    garage_config: GarageConfig,
    display_name: str,
    key_name_admin: str,
) -> JobOutcome:
    """Run the 5-step bucket + admin key flow with atomic rollback."""
    started_at = time.monotonic()
    # Throwaway alias used only for the create+rename dance. Must
    # satisfy S3 strict bucket naming (3-63 chars, lowercase
    # alphanumeric + hyphens, starts and ends alphanumeric) — Garage's
    # bucket-create validator rejects names with leading underscores
    # or any underscore at all on S3-strict deployments.
    throwaway_alias = f"provisioning-{secrets.token_hex(8)}"
    state = _ProvisionState(
        display_name=display_name, throwaway_alias=throwaway_alias,
    )

    # ---- Step 1: bucket create <throwaway> ----
    await progress("starting", 0, _TOTAL_STEPS, "Creating bucket")
    rc, _stdout, stderr = await _run_garage(
        garage_config, "bucket", "create", throwaway_alias,
    )
    if rc != 0:
        return _failure(
            failure_reason="bucket_create_failed",
            step_failed="bucket_create",
            state=state,
            stderr=stderr,
            started_at=started_at,
            rollback_status="not_required",
            extras_extra={},
        )
    state.throwaway_attached = True

    # Real Garage v2.2.0's ``bucket create`` stdout is a confirmation
    # message, NOT a bucket-info dump — parsing it via
    # ``parse_bucket_info`` extracts the throwaway alias name as the
    # bucket_id, not the actual hex UUID. Storm-side that gets
    # truncated to 16 chars and stored, then the manifest reports the
    # real UUID and the join key never matches, so the reconciler
    # nukes every just-provisioned bucket.
    #
    # Do an explicit ``bucket info <throwaway>`` call to get the
    # canonical 64-char UUID. The throwaway alias is attached at this
    # point and is the only way to address the bucket until step 11.
    rc, stdout, stderr = await _run_garage(
        garage_config, "bucket", "info", throwaway_alias,
    )
    if rc != 0:
        manual = await _delete_bucket_best_effort(
            garage_config, throwaway_alias,
        )
        return _failure(
            failure_reason="bucket_create_failed",
            step_failed="bucket_create",
            state=state,
            stderr=f"bucket info after create failed: {stderr}",
            started_at=started_at,
            rollback_status="complete" if not manual else "partial",
            extras_extra={"manual_cleanup_required": manual},
        )
    try:
        info = parse_bucket_info(stdout)
    except GarageParseError as exc:
        manual = await _delete_bucket_best_effort(
            garage_config, throwaway_alias,
        )
        return _failure(
            failure_reason="bucket_create_failed",
            step_failed="bucket_create",
            state=state,
            stderr=f"Could not parse bucket UUID: {exc}",
            started_at=started_at,
            rollback_status="complete" if not manual else "partial",
            extras_extra={
                "manual_cleanup_required": manual,
            },
        )
    state.bucket_uuid = info.bucket_id
    state.step_completed = "bucket_create"

    # ---- Step 2: key create <admin> ----
    await progress("running", 1, _TOTAL_STEPS, "Creating admin key")
    rc, stdout, stderr = await _run_garage(
        garage_config, "key", "create", key_name_admin,
    )
    if rc != 0:
        rollback = await _rollback(garage_config, state)
        return _failure(
            failure_reason="admin_key_create_failed",
            step_failed="admin_key_create",
            state=state,
            stderr=stderr,
            started_at=started_at,
            rollback_status=rollback.status,
            extras_extra={
                "manual_cleanup_required": rollback.manual_cleanup,
            },
        )
    try:
        key_result = parse_key_create(stdout)
    except GarageParseError as exc:
        rollback = await _rollback(garage_config, state)
        manual = list(rollback.manual_cleanup)
        manual.append({"type": "key_unknown_id", "name": key_name_admin})
        return _failure(
            failure_reason="admin_key_create_failed",
            step_failed="admin_key_create",
            state=state,
            stderr=f"Could not parse key ID: {exc}",
            started_at=started_at,
            rollback_status="partial",
            extras_extra={
                "manual_cleanup_required": manual,
            },
        )
    admin = _AdminKey(
        key_id=key_result.key_id,
        secret_key=key_result.secret_key,
        key_name=key_name_admin,
    )
    state.admin_key_id = admin.key_id
    state.step_completed = "admin_key_create"

    # ---- Step 3: bucket allow <flags> <throwaway> --key <admin> ----
    await progress(
        "running", 2, _TOTAL_STEPS, "Granting admin permissions",
    )
    rc, _stdout, stderr = await _run_garage(
        garage_config,
        "bucket", "allow", *_ADMIN_FLAGS,
        throwaway_alias, "--key", admin.key_id,
    )
    if rc != 0:
        rollback = await _rollback(garage_config, state)
        return _failure(
            failure_reason="admin_permission_grant_failed",
            step_failed="admin_permission_grant",
            state=state,
            stderr=stderr,
            started_at=started_at,
            rollback_status=rollback.status,
            extras_extra={
                "manual_cleanup_required": rollback.manual_cleanup,
            },
        )
    state.admin_perms_granted = True
    state.step_completed = "admin_permission_grant"

    # ---- Step 4: bucket alias --local <admin> <throwaway> <display> ----
    await progress(
        "running", 3, _TOTAL_STEPS, "Attaching admin local alias",
    )
    rc, _stdout, stderr = await _run_garage(
        garage_config,
        "bucket", "alias", "--local", admin.key_id,
        throwaway_alias, display_name,
    )
    if rc != 0:
        rollback = await _rollback(garage_config, state)
        return _failure(
            failure_reason="admin_local_alias_attach_failed",
            step_failed="admin_local_alias_attach",
            state=state,
            stderr=stderr,
            started_at=started_at,
            rollback_status=rollback.status,
            extras_extra={
                "manual_cleanup_required": rollback.manual_cleanup,
            },
        )
    state.admin_alias_attached = True
    state.step_completed = "admin_local_alias_attach"

    # ---- Step 5: bucket unalias <throwaway> (last step) ----
    # The admin's local alias satisfies Garage's "must have at least
    # one alias" orphan rule, so removing the throwaway succeeds.
    await progress(
        "running", 4, _TOTAL_STEPS, "Removing throwaway alias",
    )
    rc, _stdout, stderr = await _run_garage(
        garage_config, "bucket", "unalias", throwaway_alias,
    )
    if rc != 0:
        rollback = await _rollback(garage_config, state)
        return _failure(
            failure_reason="unalias_throwaway_failed",
            step_failed="unalias_throwaway",
            state=state,
            stderr=stderr,
            started_at=started_at,
            rollback_status=rollback.status,
            extras_extra={
                "manual_cleanup_required": rollback.manual_cleanup,
            },
        )
    state.throwaway_attached = False
    state.step_completed = "unalias_throwaway"

    # ---- Success ----
    await progress("finalizing", _TOTAL_STEPS, _TOTAL_STEPS, "Provisioning complete")
    # Expose the 16-char prefix to downstream callers — Garage CLI
    # accepts it as a bucket reference, but rejects the full 64-char
    # form. Storm stores this in CustomerBucket.garage_bucket_id.
    bucket_uuid_short = state.bucket_uuid[:16]
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=_render_success_stdout(bucket_uuid_short, admin),
        extras={
            "bucket_uuid": bucket_uuid_short,
            "admin": _key_payload(admin),
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
    garage_config: GarageConfig, state: _ProvisionState,
) -> _RollbackResult:
    """Reverse-order cleanup. Halt on first cleanup failure.

    Order:
      1. Detach the admin local alias if attached.
      2. Revoke admin permissions if granted.
      3. Delete the admin key if created.
      4. Delete the bucket if it exists.

    The throwaway alias is still attached throughout rollback, so
    bucket references in CLI calls always use ``throwaway_alias``.

    On any failure, halt and return what's still alive in
    ``manual_cleanup``.
    """
    manual: list[dict[str, Any]] = []

    # 1. Detach admin local alias
    if state.admin_alias_attached and state.admin_key_id is not None:
        try:
            rc, _stdout, _stderr = await _run_garage(
                garage_config,
                "bucket", "unalias", "--local", state.admin_key_id,
                state.display_name,
            )
        except (asyncio.TimeoutError, OSError) as exc:
            logger.warning(
                "Rollback: unalias --local %s failed: %s",
                state.admin_key_id, exc,
            )
            manual.extend(_remaining_after_alias_halt(state))
            return _RollbackResult(status="partial", manual_cleanup=manual)
        if rc != 0:
            manual.extend(_remaining_after_alias_halt(state))
            return _RollbackResult(status="partial", manual_cleanup=manual)

    # 2. Revoke admin permissions
    if state.admin_perms_granted and state.admin_key_id is not None:
        try:
            rc, _stdout, _stderr = await _run_garage(
                garage_config,
                "bucket", "deny", *_ADMIN_FLAGS,
                state.throwaway_alias, "--key", state.admin_key_id,
            )
        except (asyncio.TimeoutError, OSError) as exc:
            logger.warning(
                "Rollback: bucket deny %s failed: %s",
                state.admin_key_id, exc,
            )
            manual.extend(_remaining_after_perm_halt(state))
            return _RollbackResult(status="partial", manual_cleanup=manual)
        if rc != 0:
            manual.extend(_remaining_after_perm_halt(state))
            return _RollbackResult(status="partial", manual_cleanup=manual)

    # 3. Delete admin key
    if state.admin_key_id is not None:
        try:
            rc, _stdout, _stderr = await _run_garage(
                garage_config, "key", "delete", "--yes", state.admin_key_id,
            )
        except (asyncio.TimeoutError, OSError) as exc:
            logger.warning(
                "Rollback: key delete %s failed: %s",
                state.admin_key_id, exc,
            )
            manual.extend(_remaining_after_key_halt(state))
            return _RollbackResult(status="partial", manual_cleanup=manual)
        if rc != 0:
            manual.extend(_remaining_after_key_halt(state))
            return _RollbackResult(status="partial", manual_cleanup=manual)

    # 4. Delete bucket (always by throwaway alias)
    if state.bucket_uuid is not None:
        try:
            rc, _stdout, _stderr = await _run_garage(
                garage_config,
                "bucket", "delete", "--yes", state.throwaway_alias,
            )
        except (asyncio.TimeoutError, OSError) as exc:
            logger.warning(
                "Rollback: bucket delete %s failed: %s",
                state.throwaway_alias, exc,
            )
            manual.append({"type": "bucket", "id": state.bucket_uuid[:16]})
            return _RollbackResult(status="partial", manual_cleanup=manual)
        if rc != 0:
            manual.append({"type": "bucket", "id": state.bucket_uuid[:16]})
            return _RollbackResult(status="partial", manual_cleanup=manual)

    return _RollbackResult(status="complete", manual_cleanup=[])


def _remaining_after_alias_halt(
    state: _ProvisionState,
) -> list[dict[str, Any]]:
    """Manual-cleanup list when alias detachment halts."""
    items: list[dict[str, Any]] = []
    if state.admin_key_id is not None:
        items.append({
            "type": "local_alias",
            "key_id": state.admin_key_id,
            "alias": state.display_name,
        })
        if state.admin_perms_granted:
            items.append({
                "type": "permission_grant",
                "key_id": state.admin_key_id,
                "flags": list(_ADMIN_FLAGS),
            })
        items.append({"type": "key", "id": state.admin_key_id})
    if state.bucket_uuid is not None:
        items.append({"type": "bucket", "id": state.bucket_uuid[:16]})
    return items


def _remaining_after_perm_halt(
    state: _ProvisionState,
) -> list[dict[str, Any]]:
    """Manual-cleanup list when permission revoke halts."""
    items: list[dict[str, Any]] = []
    if state.admin_key_id is not None:
        if state.admin_perms_granted:
            items.append({
                "type": "permission_grant",
                "key_id": state.admin_key_id,
                "flags": list(_ADMIN_FLAGS),
            })
        items.append({"type": "key", "id": state.admin_key_id})
    if state.bucket_uuid is not None:
        items.append({"type": "bucket", "id": state.bucket_uuid[:16]})
    return items


def _remaining_after_key_halt(
    state: _ProvisionState,
) -> list[dict[str, Any]]:
    """Manual-cleanup list when key deletion halts."""
    items: list[dict[str, Any]] = []
    if state.admin_key_id is not None:
        items.append({"type": "key", "id": state.admin_key_id})
    if state.bucket_uuid is not None:
        items.append({"type": "bucket", "id": state.bucket_uuid[:16]})
    return items


async def _delete_bucket_best_effort(
    garage_config: GarageConfig, alias_or_uuid: str,
) -> list[dict[str, str]]:
    """Try to delete a bucket; return manual-cleanup list if it fails."""
    try:
        rc, _stdout, _stderr = await _run_garage(
            garage_config, "bucket", "delete", "--yes", alias_or_uuid,
        )
    except (asyncio.TimeoutError, OSError):
        return [{"type": "bucket", "id": alias_or_uuid}]
    if rc != 0:
        return [{"type": "bucket", "id": alias_or_uuid}]
    return []


# ---------------------------------------------------------------------------
# Result construction
# ---------------------------------------------------------------------------


def _failure(
    *,
    failure_reason: str,
    step_failed: str,
    state: _ProvisionState,
    stderr: str,
    started_at: float,
    rollback_status: str,
    extras_extra: dict[str, Any],
) -> JobOutcome:
    """Build a failure JobOutcome with the contracted extras shape."""
    bucket_uuid_short = (
        state.bucket_uuid[:16] if state.bucket_uuid else None
    )
    extras: dict[str, Any] = {
        "bucket_uuid": bucket_uuid_short,
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


def _key_payload(key: _AdminKey) -> dict[str, str]:
    return {
        "key_id": key.key_id,
        "secret": key.secret_key,
        "key_name": key.key_name,
    }


def _render_success_stdout(bucket_uuid: str, admin: _AdminKey) -> str:
    """Human-readable summary; the structured payload rides in extras."""
    return "\n".join([
        f"Bucket UUID: {bucket_uuid}",
        f"Admin key: {admin.key_id}",
    ])


def _elapsed(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)
