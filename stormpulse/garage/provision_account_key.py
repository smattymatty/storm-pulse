"""Handler for ``garage_provision_account_key``.

Mints a BUCKETS-012 account key: one ``CreateKey`` with the key-level
``allow_create_bucket`` capability set, so the customer's own S3 tooling
(aws cli, terraform, rclone) can create, own, use, and delete buckets over
the public S3 endpoint. The one-time secret rides back in the JobOutcome and
is never logged or stored (ADR buckets/002).

One forward step, no rollback: a failed ``CreateKey`` leaves nothing behind,
and a successful one is the whole job. Unlike the per-bucket key handlers,
this touches no bucket and grants no per-bucket permission - an account key
owns no bucket until the customer creates one over S3, at which point Garage
grants it ALL_PERMISSIONS on what it made (the lifecycle the single flag buys).

All Garage interaction is the admin HTTP API (ADR garage/001), never the CLI.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.garage import admin_api
from stormpulse.garage.config import GarageConfig

logger = logging.getLogger(__name__)

_TOTAL_STEPS = 1


def make_provision_account_key_handler(
    garage_config: GarageConfig,
    params: dict[str, str],
) -> JobHandler | None:
    """Build a JobHandler from runtime params.

    Required param: ``new_key_name``. Returns ``None`` if it is missing.
    """
    new_key_name = params.get("new_key_name")
    if not new_key_name:
        logger.error("garage_provision_account_key missing required param: new_key_name")
        return None

    # BUCKETS-016: the account-key tier governs create capability. An Admin key
    # is minted with create enabled (the website sends 'true'); a Read-Write /
    # Read-Only key with 'false'. FAIL CLOSED: an absent or unrecognized signal
    # yields a powerless key, never a root one, so deploy skew can never silently
    # grant create on a read tier.
    allow_create_bucket = _coerce_bool(params.get("allow_create_bucket"))

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_provision_account_key(
            progress=progress,
            garage_config=garage_config,
            new_key_name=new_key_name,
            allow_create_bucket=allow_create_bucket,
        )

    return handler


def _coerce_bool(value: Any) -> bool:
    """Params cross the wire as JSON; accept a real bool or a "true"/"false"
    string. FAIL CLOSED: only an explicit truthy value grants the capability;
    a missing/None/unrecognized value is False, so create is never granted by
    accident (BUCKETS-016)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


async def run_provision_account_key(
    progress: ProgressCallback,
    garage_config: GarageConfig,
    new_key_name: str,
    allow_create_bucket: bool = True,
) -> JobOutcome:
    """Create one account key, return its one-time secret. Single step, no
    rollback. ``allow_create_bucket`` is the BUCKETS-016 tier gate: True for an
    Admin key (the lifecycle credential), False for a Read-Write / Read-Only key
    that reaches buckets only through attach and can never create one.
    """
    started_at = time.monotonic()

    admin_url, admin_token = garage_config.admin_url, garage_config.admin_token
    if not (admin_url and admin_token):
        # Fail loud: a migrated operation never silently no-ops (ADR garage/001).
        return _failure(
            failure_reason="admin_api_unconfigured",
            new_key_id=None,
            stderr=(
                "Garage admin API not configured (admin_url + admin_token); set "
                "[garage] admin_url and admin_token_file."
            ),
            started_at=started_at,
            extras_extra={},
        )

    # ---- Step 1: CreateKey with the tier's create capability ----
    await progress("starting", 0, _TOTAL_STEPS, "Creating account key")
    info, err = admin_api.create_key(
        admin_url=admin_url,
        admin_token=admin_token,
        name=new_key_name,
        allow_create_bucket=allow_create_bucket,
    )
    if info is None:
        return _failure(
            failure_reason="account_key_create_failed",
            new_key_id=None,
            stderr=err,
            started_at=started_at,
            extras_extra={},
        )
    new_key_id = info.get("accessKeyId") or ""
    new_secret = info.get("secretAccessKey") or ""
    if not new_key_id:
        # The key was created but the response didn't identify it; we can't
        # roll it back without its id, so flag it for manual cleanup.
        return _failure(
            failure_reason="account_key_create_failed",
            new_key_id=None,
            stderr="CreateKey response missing accessKeyId",
            started_at=started_at,
            extras_extra={
                "manual_cleanup_required": [
                    {"type": "key_unknown_id", "name": new_key_name},
                ],
            },
        )

    # ---- Success ----
    await progress("finalizing", _TOTAL_STEPS, _TOTAL_STEPS, "Account key created")
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"New account key: {new_key_id}",
        extras={
            "new_key_id": new_key_id,
            "new_secret": new_secret,
            "new_key_name": new_key_name,
            "can_create_bucket": allow_create_bucket,
            "step_completed": "account_key_create",
            "step_failed": None,
            "rollback_status": "not_required",
            "manual_cleanup_required": [],
            "garage_stderr": "",
            "duration_seconds": _elapsed(started_at),
        },
    )


def _failure(
    *,
    failure_reason: str,
    new_key_id: str | None,
    stderr: str,
    started_at: float,
    extras_extra: dict[str, Any],
) -> JobOutcome:
    extras: dict[str, Any] = {
        "new_key_id": new_key_id,
        "can_create_bucket": True,
        "step_completed": None,
        "step_failed": "account_key_create",
        "rollback_status": "not_required",
        "manual_cleanup_required": [],
        "garage_stderr": stderr,
        "duration_seconds": _elapsed(started_at),
    }
    extras.update(extras_extra)
    return JobOutcome(
        success=False,
        exit_code=-1,
        stdout="",
        stderr=stderr,
        failure_reason=failure_reason,
        extras=extras,
    )


def _elapsed(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)
