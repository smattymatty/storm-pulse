"""Handler for the ``garage_provision_customer_bucket`` long-running command.

The dashboard dispatches a single ``command.request`` with the customer's
bucket display name and the three key names (admin / rw / ro). This handler
performs the full 11-step provisioning flow, captures the bucket UUID via
``parse_bucket_info``, and returns the three key IDs and secrets in a
single result. On any partial failure, it rolls back to the pre-call state.

The contract — step ordering, rollback table, failure-reason vocabulary,
and idempotency rule — is documented in
``_architecture/specs/cellar-bucket-naming-foundation.md`` (Issue 4).
This module implements that contract; deviations are bugs.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
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

_TOTAL_STEPS = 11


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
    required = ("display_name", "key_name_admin", "key_name_rw", "key_name_ro")
    if not all(params.get(k) for k in required):
        logger.error(
            "garage_provision_customer_bucket missing required params: %s",
            [k for k in required if not params.get(k)],
        )
        return None

    display_name = params["display_name"]
    key_name_admin = params["key_name_admin"]
    key_name_rw = params["key_name_rw"]
    key_name_ro = params["key_name_ro"]

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_provision_customer_bucket(
            progress=progress,
            garage_config=garage_config,
            display_name=display_name,
            key_name_admin=key_name_admin,
            key_name_rw=key_name_rw,
            key_name_ro=key_name_ro,
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
    keys_created: list[str] = field(default_factory=list)
    permissions_granted: list[tuple[str, list[str]]] = field(
        default_factory=list,
    )
    local_aliases_attached: list[str] = field(default_factory=list)
    step_completed: str | None = None


@dataclass(frozen=True)
class _KeyTriple:
    """Captured key info from steps 2-4."""

    key_id: str
    secret_key: str
    key_name: str


_KEY_LABELS = ("admin", "rw", "ro")


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
    key_name_rw: str,
    key_name_ro: str,
) -> JobOutcome:
    """Run the 11-step provisioning flow with rollback.

    Tests inject ``garage_config`` pointing at a fake docker/garage path
    plus a custom ``_run_garage`` (via monkeypatch) to simulate failures.
    """
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
    keys: list[_KeyTriple] = []

    # ---- Step 1: bucket create <throwaway> ----
    await progress("starting", 0, _TOTAL_STEPS, "Creating bucket")
    rc, stdout, stderr = await _run_garage(
        garage_config, "bucket", "create", throwaway_alias,
    )
    if rc != 0:
        # Nothing was created. No rollback needed.
        return _failure(
            failure_reason="bucket_create_failed",
            step_failed="bucket_create",
            state=state,
            stderr=stderr,
            started_at=started_at,
            rollback_status="not_required",
            extras_extra={},
        )
    try:
        info = parse_bucket_info(stdout)
    except GarageParseError as exc:
        # The bucket exists but we can't extract the UUID. Best-effort
        # cleanup by alias, then report.
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
    state.throwaway_attached = True
    state.step_completed = "bucket_create"

    # ---- Steps 2-4: key creates ----
    key_names = (key_name_admin, key_name_rw, key_name_ro)
    for idx, key_name in enumerate(key_names):
        label = _KEY_LABELS[idx]
        await progress(
            "running", 1 + idx, _TOTAL_STEPS, f"Creating {label} key",
        )
        rc, stdout, stderr = await _run_garage(
            garage_config, "key", "create", key_name,
        )
        if rc != 0:
            rollback = await _rollback(garage_config, state)
            return _failure(
                failure_reason="key_create_failed",
                step_failed=f"key_create_{label}",
                state=state,
                stderr=stderr,
                started_at=started_at,
                rollback_status=rollback.status,
                extras_extra={
                    "step_index": idx,
                    "manual_cleanup_required": rollback.manual_cleanup,
                },
            )
        try:
            key_result = parse_key_create(stdout)
        except GarageParseError as exc:
            # The key was created but we can't extract its ID. We can't
            # delete it during rollback because we don't know the ID.
            # Log it; the operator will have to clean it up by name.
            rollback = await _rollback(garage_config, state)
            manual = list(rollback.manual_cleanup)
            manual.append({"type": "key_unknown_id", "name": key_name})
            return _failure(
                failure_reason="key_create_failed",
                step_failed=f"key_create_{label}",
                state=state,
                stderr=f"Could not parse key ID: {exc}",
                started_at=started_at,
                rollback_status="partial",
                extras_extra={
                    "step_index": idx,
                    "manual_cleanup_required": manual,
                },
            )
        keys.append(_KeyTriple(
            key_id=key_result.key_id,
            secret_key=key_result.secret_key,
            key_name=key_name,
        ))
        state.keys_created.append(key_result.key_id)
        state.step_completed = f"key_create_{label}"

    # ---- Steps 5-7: permission grants ----
    perm_flags = (
        ["--read", "--write", "--owner"],
        ["--read", "--write"],
        ["--read"],
    )
    for idx, flags in enumerate(perm_flags):
        label = _KEY_LABELS[idx]
        await progress(
            "running", 4 + idx, _TOTAL_STEPS,
            f"Granting {label} permissions",
        )
        rc, _stdout, stderr = await _run_garage(
            garage_config,
            "bucket", "allow", *flags,
            throwaway_alias, "--key", keys[idx].key_id,
        )
        if rc != 0:
            rollback = await _rollback(garage_config, state)
            return _failure(
                failure_reason="permission_grant_failed",
                step_failed=f"permission_grant_{label}",
                state=state,
                stderr=stderr,
                started_at=started_at,
                rollback_status=rollback.status,
                extras_extra={
                    "step_index": idx,
                    "manual_cleanup_required": rollback.manual_cleanup,
                },
            )
        state.permissions_granted.append((keys[idx].key_id, list(flags)))
        state.step_completed = f"permission_grant_{label}"

    # ---- Steps 8-10: local alias attaches ----
    for idx in range(3):
        label = _KEY_LABELS[idx]
        await progress(
            "running", 7 + idx, _TOTAL_STEPS,
            f"Attaching {label} local alias",
        )
        rc, _stdout, stderr = await _run_garage(
            garage_config,
            "bucket", "alias", "--local", keys[idx].key_id,
            throwaway_alias, display_name,
        )
        if rc != 0:
            rollback = await _rollback(garage_config, state)
            return _failure(
                failure_reason="local_alias_attach_failed",
                step_failed=f"local_alias_attach_{label}",
                state=state,
                stderr=stderr,
                started_at=started_at,
                rollback_status=rollback.status,
                extras_extra={
                    "step_index": idx,
                    "manual_cleanup_required": rollback.manual_cleanup,
                },
            )
        state.local_aliases_attached.append(keys[idx].key_id)
        state.step_completed = f"local_alias_attach_{label}"

    # ---- Step 11: bucket unalias <throwaway> (last step) ----
    # The throwaway is the bucket's only global alias at this point;
    # the 3 local aliases satisfy Garage's "must have at least one
    # alias" orphan rule, so this unalias succeeds. If it does fail
    # for some reason, atomic rollback runs (Change C).
    await progress(
        "running", 10, _TOTAL_STEPS, "Removing throwaway alias",
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
            extras_extra={"manual_cleanup_required": rollback.manual_cleanup},
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
        stdout=_render_success_stdout(bucket_uuid_short, keys),
        extras={
            "bucket_uuid": bucket_uuid_short,
            "admin": _key_payload(keys[0]),
            "rw": _key_payload(keys[1]),
            "ro": _key_payload(keys[2]),
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
      1. Detach any local aliases that were attached.
      2. Revoke any permissions that were granted (deny).
      3. Delete any keys that were created.
      4. Delete the bucket if it exists.

    The throwaway alias is still attached throughout rollback (it's
    only removed by step 11 in the success path; rollback runs only
    when an earlier step failed OR step 11 itself failed, in which
    case the throwaway is also still attached). So bucket references
    in rollback CLI calls always use ``throwaway_alias``.

    On any failure, halt and return what's still alive in
    ``manual_cleanup``.
    """
    manual: list[dict[str, Any]] = []

    # 1. Detach local aliases
    for key_id in state.local_aliases_attached:
        try:
            rc, _stdout, _stderr = await _run_garage(
                garage_config,
                "bucket", "unalias", "--local", key_id,
                state.display_name,
            )
        except (asyncio.TimeoutError, OSError) as exc:
            logger.warning("Rollback: unalias --local %s failed: %s", key_id, exc)
            manual.extend(_remaining_after_alias_halt(state, key_id))
            return _RollbackResult(status="partial", manual_cleanup=manual)
        if rc != 0:
            manual.extend(_remaining_after_alias_halt(state, key_id))
            return _RollbackResult(status="partial", manual_cleanup=manual)

    # 2. Revoke permissions
    for key_id, flags in state.permissions_granted:
        try:
            rc, _stdout, _stderr = await _run_garage(
                garage_config,
                "bucket", "deny", *flags,
                state.throwaway_alias, "--key", key_id,
            )
        except (asyncio.TimeoutError, OSError) as exc:
            logger.warning("Rollback: bucket deny %s failed: %s", key_id, exc)
            manual.extend(_remaining_after_perm_halt(state, key_id))
            return _RollbackResult(status="partial", manual_cleanup=manual)
        if rc != 0:
            manual.extend(_remaining_after_perm_halt(state, key_id))
            return _RollbackResult(status="partial", manual_cleanup=manual)

    # 3. Delete keys
    for key_id in state.keys_created:
        try:
            rc, _stdout, _stderr = await _run_garage(
                garage_config, "key", "delete", "--yes", key_id,
            )
        except (asyncio.TimeoutError, OSError) as exc:
            logger.warning("Rollback: key delete %s failed: %s", key_id, exc)
            manual.extend(_remaining_after_key_halt(state, key_id))
            return _RollbackResult(status="partial", manual_cleanup=manual)
        if rc != 0:
            manual.extend(_remaining_after_key_halt(state, key_id))
            return _RollbackResult(status="partial", manual_cleanup=manual)

    # 4. Delete bucket (always by throwaway alias; throwaway is
    # always still attached during rollback).
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
    state: _ProvisionState, halted_at_key_id: str,
) -> list[dict[str, Any]]:
    """Manual-cleanup list when alias detachment halts at the given key."""
    items: list[dict[str, Any]] = []
    halt_idx = state.local_aliases_attached.index(halted_at_key_id)
    # Aliases still attached (this one and onward)
    for kid in state.local_aliases_attached[halt_idx:]:
        items.append({"type": "local_alias", "key_id": kid,
                      "alias": state.display_name})
    # All permissions still granted (none have been revoked yet)
    for kid, flags in state.permissions_granted:
        items.append({"type": "permission_grant", "key_id": kid,
                      "flags": flags})
    # All keys still alive
    for kid in state.keys_created:
        items.append({"type": "key", "id": kid})
    if state.bucket_uuid is not None:
        items.append({"type": "bucket", "id": state.bucket_uuid[:16]})
    return items


def _remaining_after_perm_halt(
    state: _ProvisionState, halted_at_key_id: str,
) -> list[dict[str, Any]]:
    """Manual-cleanup list when permission revoke halts at the given key."""
    items: list[dict[str, Any]] = []
    halt_idx = next(
        i for i, (kid, _) in enumerate(state.permissions_granted)
        if kid == halted_at_key_id
    )
    # Permissions still granted (this one and onward)
    for kid, flags in state.permissions_granted[halt_idx:]:
        items.append({"type": "permission_grant", "key_id": kid,
                      "flags": flags})
    # All keys still alive
    for kid in state.keys_created:
        items.append({"type": "key", "id": kid})
    if state.bucket_uuid is not None:
        items.append({"type": "bucket", "id": state.bucket_uuid[:16]})
    return items


def _remaining_after_key_halt(
    state: _ProvisionState, halted_at_key_id: str,
) -> list[dict[str, Any]]:
    """Manual-cleanup list when key deletion halts at the given key."""
    items: list[dict[str, Any]] = []
    halt_idx = state.keys_created.index(halted_at_key_id)
    for kid in state.keys_created[halt_idx:]:
        items.append({"type": "key", "id": kid})
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
    # Expose the 16-char prefix to downstream callers, matching the
    # success path. Garage CLI accepts this as a bucket reference;
    # the full 64-char form is rejected.
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
    # Per the contract: when rollback itself errors partway, the failure
    # reason flips to ``rollback_failed`` and ``step_failed`` retains the
    # originating step.
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


def _key_payload(key: _KeyTriple) -> dict[str, str]:
    return {
        "key_id": key.key_id,
        "secret": key.secret_key,
        "key_name": key.key_name,
    }


def _render_success_stdout(bucket_uuid: str | None, keys: list[_KeyTriple]) -> str:
    """Human-readable summary; the structured payload rides in extras."""
    lines = [
        f"Bucket UUID: {bucket_uuid}",
        f"Admin key: {keys[0].key_id}",
        f"RW key: {keys[1].key_id}",
        f"RO key: {keys[2].key_id}",
    ]
    return "\n".join(lines)


def _elapsed(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)
