"""Handler for ``garage_delete_provisioned_bucket``.

Garage v2.2.0's CLI has a deadlock: ``bucket delete --yes`` rejects buckets
with local aliases, and ``bucket unalias`` rejects detaching the last alias
as an orphan. We break it by attaching a temporary global alias, detaching
every local, then deleting via the global (which goes with the bucket).

Idempotent on NoSuchBucket. Step 5 (orphaned key cleanup) is best-effort:
failures accumulate in ``manual_cleanup_required`` rather than failing the
orchestrator.
"""

from __future__ import annotations

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
    parse_key_info,
)
from stormpulse.garage.runner import run_garage  # reuse helper

logger = logging.getLogger(__name__)


_TOTAL_STEPS = 5


def make_delete_provisioned_bucket_handler(
    garage_config: GarageConfig,
    params: dict[str, str],
) -> JobHandler | None:
    """Build a JobHandler from runtime params.

    Required: ``bucket_id``. Returns ``None`` if missing.
    """
    if not params.get("bucket_id"):
        logger.error(
            "garage_delete_provisioned_bucket missing required param: bucket_id",
        )
        return None
    bucket_id = params["bucket_id"]

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_delete_provisioned_bucket(
            progress=progress,
            garage_config=garage_config,
            bucket_id=bucket_id,
        )

    return handler


@dataclass
class _DeleteState:
    """What's been done so far, for rollback."""

    bucket_id: str
    temp_global: str | None = None
    temp_global_attached: bool = False
    locals_detached: list[tuple[str, str]] = field(default_factory=list)
    globals_detached: list[str] = field(default_factory=list)
    final_ref: str | None = None
    # Keys that had permissions on the bucket at step 1. Step 5
    # iterates this list to clean up any that are now unmoored.
    candidate_key_ids: list[str] = field(default_factory=list)
    keys_deleted: list[str] = field(default_factory=list)
    keys_skipped: list[str] = field(default_factory=list)
    step_completed: str | None = None


async def run_delete_provisioned_bucket(
    progress: ProgressCallback,
    garage_config: GarageConfig,
    bucket_id: str,
) -> JobOutcome:
    """Run the delete flow with atomic rollback."""
    started_at = time.monotonic()
    state = _DeleteState(bucket_id=bucket_id)

    # ---- Step 1: bucket info ----
    await progress("starting", 0, _TOTAL_STEPS, "Reading bucket state")
    rc, stdout, stderr = await run_garage(
        garage_config,
        "bucket",
        "info",
        bucket_id,
    )
    if rc != 0:
        # Idempotent: if the bucket already doesn't exist, success.
        if "NoSuchBucket" in stderr or "no such bucket" in stderr.lower():
            return _success_already_gone(state, started_at)
        return _failure(
            failure_reason="bucket_info_failed",
            step_failed="bucket_info",
            state=state,
            stderr=stderr,
            started_at=started_at,
            rollback_status="not_required",
        )
    try:
        info = parse_bucket_info(stdout)
    except GarageParseError as exc:
        return _failure(
            failure_reason="bucket_info_failed",
            step_failed="bucket_info",
            state=state,
            stderr=f"Could not parse bucket info: {exc}",
            started_at=started_at,
            rollback_status="not_required",
        )

    # Early exit: non-empty buckets cannot be deleted. Fail here before
    # mutating any Garage state (no temp alias attached, no locals
    # detached), so the only side effect of the call is the bucket-info
    # read. Without this, the orchestrator would do steps 2-3 of state
    # changes, then trip ``BucketNotEmpty`` at step 4 and rollback - same
    # outcome, more work, more chances for the rollback itself to leave
    # residue.
    if info.object_count > 0:
        return _failure(
            failure_reason="bucket_not_empty",
            step_failed="bucket_info",
            state=state,
            stderr=(
                f"Bucket has {info.object_count} object(s). "
                f"Clear the bucket before deleting."
            ),
            started_at=started_at,
            rollback_status="not_required",
        )

    # Enumerate aliases. parse_bucket_info exposes a single global_alias
    # field (rare to have more than one); locals come from the keys
    # list with non-empty local_alias.
    existing_global = info.global_alias.strip() if info.global_alias else ""
    local_aliases = [
        (k.access_key_id, k.local_alias) for k in info.keys if k.local_alias
    ]
    # Capture every key that had permissions or a local alias on this
    # bucket - step 5 will check each one with ``key info`` to decide
    # whether to delete it or leave it alone (shared key on another
    # bucket).
    state.candidate_key_ids = [k.access_key_id for k in info.keys]
    state.step_completed = "bucket_info"

    # ---- Step 2: ensure at least one global alias ----
    # The bucket must end this step with a global alias we can use as
    # the final delete reference. If one already exists, use it; else
    # attach a temporary one. Locals can't be used as the final ref
    # because ``bucket delete --yes`` doesn't accept local-alias
    # syntax (locals are key-scoped).
    await progress(
        "running",
        1,
        _TOTAL_STEPS,
        "Preparing teardown",
    )
    if existing_global:
        state.final_ref = existing_global
    else:
        state.temp_global = f"pulse-delete-{secrets.token_hex(6)}"
        rc, _stdout, stderr = await run_garage(
            garage_config,
            "bucket",
            "alias",
            bucket_id,
            state.temp_global,
        )
        if rc != 0:
            return _failure(
                failure_reason="temp_alias_attach_failed",
                step_failed="temp_alias_attach",
                state=state,
                stderr=stderr,
                started_at=started_at,
                rollback_status="not_required",
            )
        state.temp_global_attached = True
        state.final_ref = state.temp_global
    state.step_completed = "temp_alias_attach"

    # ---- Step 3: detach all local aliases ----
    # Orphan rule allows this because we still have at least one
    # global alias attached (real or temp).
    await progress(
        "running",
        2,
        _TOTAL_STEPS,
        "Detaching local aliases",
    )
    for key_id, name in local_aliases:
        rc, _stdout, stderr = await run_garage(
            garage_config,
            "bucket",
            "unalias",
            "--local",
            key_id,
            name,
        )
        if rc != 0:
            rollback = await _rollback(garage_config, state)
            return _failure(
                failure_reason="local_alias_detach_failed",
                step_failed="local_alias_detach",
                state=state,
                stderr=stderr,
                started_at=started_at,
                rollback_status=rollback.status,
                extras_extra={
                    "manual_cleanup_required": rollback.manual_cleanup,
                },
            )
        state.locals_detached.append((key_id, name))
    state.step_completed = "local_alias_detach"

    # ---- Step 4: bucket delete --yes <final-ref> ----
    await progress(
        "running",
        3,
        _TOTAL_STEPS,
        "Deleting bucket",
    )
    rc, _stdout, stderr = await run_garage(
        garage_config,
        "bucket",
        "delete",
        "--yes",
        state.final_ref,
    )
    if rc != 0:
        # BucketNotEmpty is a customer-actionable error - surface it
        # without rolling back partial alias state, since rolling back
        # would re-attach the locals and the customer needs to clear
        # the bucket first anyway.
        if "BucketNotEmpty" in stderr:
            rollback = await _rollback(garage_config, state)
            return _failure(
                failure_reason="bucket_not_empty",
                step_failed="bucket_delete",
                state=state,
                stderr=stderr,
                started_at=started_at,
                rollback_status=rollback.status,
                extras_extra={
                    "manual_cleanup_required": rollback.manual_cleanup,
                },
            )
        rollback = await _rollback(garage_config, state)
        return _failure(
            failure_reason="bucket_delete_failed",
            step_failed="bucket_delete",
            state=state,
            stderr=stderr,
            started_at=started_at,
            rollback_status=rollback.status,
            extras_extra={
                "manual_cleanup_required": rollback.manual_cleanup,
            },
        )
    state.step_completed = "bucket_delete"

    # ---- Step 5: clean up unmoored keys (best-effort) ----
    # The bucket is already gone. For each key that had access to it,
    # check whether it has any other buckets. If not, the key is
    # orphaned credential material - delete it. If it still has
    # buckets (shared key, attached to multiple), leave it alone.
    # Failures here don't fail the orchestrator; they accumulate in
    # ``manual_cleanup_required``.
    await progress(
        "running",
        4,
        _TOTAL_STEPS,
        "Cleaning up unmoored keys",
    )
    manual_key_cleanup: list[dict[str, Any]] = []
    for key_id in state.candidate_key_ids:
        try:
            rc, stdout, _stderr = await run_garage(
                garage_config,
                "key",
                "info",
                key_id,
            )
        except (TimeoutError, OSError) as exc:
            logger.warning(
                "Step 5: key info %s failed (%s); leaving key alone",
                key_id,
                exc,
            )
            manual_key_cleanup.append({"type": "key", "id": key_id})
            continue
        if rc != 0:
            # Key already gone or NoSuchKey - nothing to clean up.
            state.keys_skipped.append(key_id)
            continue
        try:
            key_info = parse_key_info(stdout)
        except GarageParseError:
            manual_key_cleanup.append({"type": "key", "id": key_id})
            continue
        if key_info.buckets:
            # Key still has other buckets - preserve it.
            state.keys_skipped.append(key_id)
            continue
        # Key has zero buckets - safe to delete.
        try:
            rc, _stdout, _stderr = await run_garage(
                garage_config,
                "key",
                "delete",
                "--yes",
                key_id,
            )
        except (TimeoutError, OSError) as exc:
            logger.warning(
                "Step 5: key delete %s failed (%s)",
                key_id,
                exc,
            )
            manual_key_cleanup.append({"type": "key", "id": key_id})
            continue
        if rc != 0:
            manual_key_cleanup.append({"type": "key", "id": key_id})
            continue
        state.keys_deleted.append(key_id)
    state.step_completed = "key_cleanup"

    # ---- Success ----
    await progress("finalizing", _TOTAL_STEPS, _TOTAL_STEPS, "Bucket deleted")
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"Bucket {bucket_id[:16]} deleted",
        extras={
            "bucket_id": bucket_id[:16],
            "step_completed": state.step_completed,
            "step_failed": None,
            "rollback_status": "not_required",
            "manual_cleanup_required": manual_key_cleanup,
            "keys_deleted": state.keys_deleted,
            "keys_skipped": state.keys_skipped,
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
    state: _DeleteState,
) -> _RollbackResult:
    """Reverse-order cleanup: re-attach locals, drop temp global.

    Re-attaches in REVERSE order of detachment (LIFO). If a re-attach
    fails, halt and add the rest to ``manual_cleanup_required``.
    """
    manual: list[dict[str, Any]] = []

    # 1. Re-attach local aliases (LIFO)
    for key_id, name in reversed(state.locals_detached):
        try:
            rc, _stdout, _stderr = await run_garage(
                garage_config,
                "bucket",
                "alias",
                "--local",
                key_id,
                state.bucket_id,
                name,
            )
        except (TimeoutError, OSError) as exc:
            logger.warning(
                "Rollback: local alias re-attach %s/%s failed: %s",
                key_id,
                name,
                exc,
            )
            manual.append(
                {
                    "type": "local_alias",
                    "key_id": key_id,
                    "alias": name,
                }
            )
            continue
        if rc != 0:
            manual.append(
                {
                    "type": "local_alias",
                    "key_id": key_id,
                    "alias": name,
                }
            )

    # 2. Drop temp global if we attached one
    if state.temp_global_attached and state.temp_global:
        try:
            rc, _stdout, _stderr = await run_garage(
                garage_config,
                "bucket",
                "unalias",
                state.temp_global,
            )
        except (TimeoutError, OSError) as exc:
            logger.warning(
                "Rollback: temp global unalias %s failed: %s",
                state.temp_global,
                exc,
            )
            manual.append(
                {
                    "type": "global_alias",
                    "alias": state.temp_global,
                }
            )
        else:
            if rc != 0:
                manual.append(
                    {
                        "type": "global_alias",
                        "alias": state.temp_global,
                    }
                )

    if manual:
        return _RollbackResult(status="partial", manual_cleanup=manual)
    return _RollbackResult(status="complete", manual_cleanup=[])


def _success_already_gone(
    state: _DeleteState,
    started_at: float,
) -> JobOutcome:
    """The bucket already doesn't exist; this is success (idempotent)."""
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"Bucket {state.bucket_id[:16]} already absent",
        extras={
            "bucket_id": state.bucket_id[:16],
            "step_completed": "bucket_info",
            "step_failed": None,
            "rollback_status": "not_required",
            "manual_cleanup_required": [],
            "already_absent": True,
            "garage_stderr": "",
            "duration_seconds": _elapsed(started_at),
        },
    )


def _failure(
    *,
    failure_reason: str,
    step_failed: str,
    state: _DeleteState,
    stderr: str,
    started_at: float,
    rollback_status: str,
    extras_extra: dict[str, Any] | None = None,
) -> JobOutcome:
    extras: dict[str, Any] = {
        "bucket_id": state.bucket_id[:16],
        "step_completed": state.step_completed,
        "step_failed": step_failed,
        "rollback_status": rollback_status,
        "manual_cleanup_required": [],
        "garage_stderr": stderr,
        "duration_seconds": _elapsed(started_at),
    }
    if extras_extra:
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
