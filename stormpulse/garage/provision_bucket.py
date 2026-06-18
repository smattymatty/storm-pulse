"""Handler for ``garage_provision_customer_bucket``.

Creates a bucket and its admin key atomically via the admin HTTP API (ADR
garage/001). ``CreateKey`` runs first (the bucket's local alias needs the key
id), then a single ``CreateBucket`` call binds the admin key's local alias and
grants its read/write/owner permissions in one Garage transaction. The CLI
throwaway-alias dance is gone: the admin API addresses the bucket by id and
returns it directly, so no global alias is ever created.

Rollback is one step: if the bucket create fails, the orphan admin key is
deleted. Additional keys (rw/ro) are added later via ``provision_additional_key``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.garage import admin_api
from stormpulse.garage.config import GarageConfig

logger = logging.getLogger(__name__)


_TOTAL_STEPS = 2

_ADMIN_PERMS = {"read": True, "write": True, "owner": True}


def make_provision_customer_bucket_handler(
    garage_config: GarageConfig,
    params: dict[str, str],
) -> JobHandler | None:
    """Build a JobHandler from runtime params.

    Returns ``None`` if a required param is missing - the caller emits a
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


@dataclass(frozen=True)
class _AdminKey:
    """Captured admin key info from the CreateKey step."""

    key_id: str
    secret_key: str
    key_name: str


async def run_provision_customer_bucket(
    progress: ProgressCallback,
    garage_config: GarageConfig,
    display_name: str,
    key_name_admin: str,
) -> JobOutcome:
    """Run the 2-step atomic bucket + admin key flow."""
    started_at = time.monotonic()
    admin_url, admin_token = garage_config.admin_url, garage_config.admin_token
    if not (admin_url and admin_token):
        # Fail loud: a migrated operation never silently no-ops (ADR garage/001).
        return _failure(
            failure_reason="admin_api_unconfigured",
            step_failed=None,
            step_completed=None,
            bucket_uuid=None,
            stderr=(
                "Garage admin API not configured (admin_url + admin_token); set "
                "[garage] admin_url and admin_token_file."
            ),
            started_at=started_at,
            rollback_status="not_required",
            extras_extra={},
        )

    # ---- Step 1: CreateKey (admin) ----
    await progress("starting", 0, _TOTAL_STEPS, "Creating admin key")
    info, err = admin_api.create_key(
        admin_url=admin_url, admin_token=admin_token, name=key_name_admin,
    )
    if info is None:
        return _failure(
            failure_reason="admin_key_create_failed",
            step_failed="admin_key_create",
            step_completed=None,
            bucket_uuid=None,
            stderr=err,
            started_at=started_at,
            rollback_status="not_required",
            extras_extra={},
        )
    admin_key_id = info.get("accessKeyId") or ""
    if not admin_key_id:
        return _failure(
            failure_reason="admin_key_create_failed",
            step_failed="admin_key_create",
            step_completed=None,
            bucket_uuid=None,
            stderr="CreateKey response missing accessKeyId",
            started_at=started_at,
            rollback_status="partial",
            extras_extra={
                "manual_cleanup_required": [
                    {"type": "key_unknown_id", "name": key_name_admin},
                ],
            },
        )
    admin = _AdminKey(
        key_id=admin_key_id,
        secret_key=info.get("secretAccessKey") or "",
        key_name=key_name_admin,
    )

    # ---- Step 2: CreateBucket, binding the admin local alias + perms atomically ----
    await progress("running", 1, _TOTAL_STEPS, "Creating bucket")
    bucket, err = admin_api.create_bucket(
        admin_url=admin_url,
        admin_token=admin_token,
        local_alias={
            "accessKeyId": admin_key_id,
            "alias": display_name,
            "allow": dict(_ADMIN_PERMS),
        },
    )
    if bucket is None:
        return _failure(
            failure_reason="bucket_create_failed",
            step_failed="bucket_create",
            step_completed="admin_key_create",
            bucket_uuid=None,
            stderr=err,
            started_at=started_at,
            **_rollback_orphan_key(garage_config, admin_key_id),
        )
    bucket_uuid = bucket.get("id") or ""
    if not bucket_uuid:
        return _failure(
            failure_reason="bucket_create_failed",
            step_failed="bucket_create",
            step_completed="admin_key_create",
            bucket_uuid=None,
            stderr="CreateBucket response missing id",
            started_at=started_at,
            **_rollback_orphan_key(garage_config, admin_key_id),
        )

    # ---- Success ----
    # Expose the 16-char prefix downstream: the admin API and CLI both accept it
    # as a bucket reference. Storm stores it in CustomerBucket.garage_bucket_id.
    bucket_uuid_short = bucket_uuid[:16]
    await progress("finalizing", _TOTAL_STEPS, _TOTAL_STEPS, "Provisioning complete")
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=_render_success_stdout(bucket_uuid_short, admin),
        extras={
            "bucket_uuid": bucket_uuid_short,
            "admin": _key_payload(admin),
            "step_completed": "bucket_create",
            "step_failed": None,
            "rollback_status": "not_required",
            "manual_cleanup_required": [],
            "garage_stderr": "",
            "duration_seconds": _elapsed(started_at),
        },
    )


def _rollback_orphan_key(
    garage_config: GarageConfig,
    admin_key_id: str,
) -> dict[str, Any]:
    """Delete the orphan admin key after a failed bucket create.

    Returns the ``rollback_status`` + ``extras_extra`` kwargs for ``_failure``:
    ``complete`` (key gone) or ``partial`` (key delete failed, flagged for
    manual cleanup). admin_api never raises, so a transport failure is just a
    ``(False, _)`` here, not an exception.
    """
    ok, _err = admin_api.delete_key(
        admin_url=garage_config.admin_url,
        admin_token=garage_config.admin_token,
        access_key_id=admin_key_id,
    )
    if ok:
        return {"rollback_status": "complete", "extras_extra": {}}
    return {
        "rollback_status": "partial",
        "extras_extra": {"manual_cleanup_required": [{"type": "key", "id": admin_key_id}]},
    }


def _failure(
    *,
    failure_reason: str,
    step_failed: str | None,
    step_completed: str | None,
    bucket_uuid: str | None,
    stderr: str,
    started_at: float,
    rollback_status: str,
    extras_extra: dict[str, Any],
) -> JobOutcome:
    """Build a failure JobOutcome with the contracted extras shape."""
    extras: dict[str, Any] = {
        "bucket_uuid": bucket_uuid,
        "step_completed": step_completed,
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


def _key_payload(key: _AdminKey) -> dict[str, str]:
    return {
        "key_id": key.key_id,
        "secret": key.secret_key,
        "key_name": key.key_name,
    }


def _render_success_stdout(bucket_uuid: str, admin: _AdminKey) -> str:
    """Human-readable summary; the structured payload rides in extras."""
    return "\n".join(
        [
            f"Bucket UUID: {bucket_uuid}",
            f"Admin key: {admin.key_id}",
        ]
    )


def _elapsed(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)
