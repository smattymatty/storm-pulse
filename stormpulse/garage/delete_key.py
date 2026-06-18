"""Handler for ``garage_delete_key``.

Deletes an access key via the admin HTTP API (ADR garage/001) and reports a
*structured, confirmed* outcome, distinct from the legacy CLI
``garage_key_delete`` (``garage key delete --yes``) whose already-gone exit
is indistinguishable from a transient error.

The credential-kill tombstone sweep (website BUCKETS-013) needs exactly one
unambiguous signal: "this key is positively gone from Garage." Two results
satisfy that and are reported as success:

  - ``deleted``        - DeleteKey returned 2xx; the key existed and is gone.
  - ``already_absent`` - DeleteKey returned a positive 404 / NoSuchKey; the
                         key was already gone. Idempotent, still success.

Anything else (transport error, 5xx, auth failure) is a transient failure:
``success=False``, so the sweep leaves the tombstone open and retries next
tick rather than certifying a still-live key as dead.

One admin call, no rollback (DeleteKey is the single mutation).
"""

from __future__ import annotations

import logging
import time

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.garage import admin_api
from stormpulse.garage.config import GarageConfig

logger = logging.getLogger(__name__)


_TOTAL_STEPS = 1


def make_delete_key_handler(
    garage_config: GarageConfig,
    params: dict[str, str],
) -> JobHandler | None:
    """Build a JobHandler from runtime params.

    Required: ``key_id``. Returns ``None`` if missing.
    """
    if not params.get("key_id"):
        logger.error("garage_delete_key missing required param: key_id")
        return None
    key_id = params["key_id"]

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_delete_key(
            progress=progress,
            garage_config=garage_config,
            key_id=key_id,
        )

    return handler


async def run_delete_key(
    progress: ProgressCallback,
    garage_config: GarageConfig,
    key_id: str,
) -> JobOutcome:
    """Delete the key and report whether it is positively gone."""
    started_at = time.monotonic()
    admin_url, admin_token = garage_config.admin_url, garage_config.admin_token
    if not (admin_url and admin_token):
        # Fail loud: a migrated operation never silently no-ops (ADR garage/001).
        return _failure(
            key_id=key_id,
            stderr=(
                "Garage admin API not configured (admin_url + admin_token); set "
                "[garage] admin_url and admin_token_file."
            ),
            started_at=started_at,
        )

    await progress("starting", 0, _TOTAL_STEPS, "Deleting key")
    ok, err = admin_api.delete_key(
        admin_url=admin_url, admin_token=admin_token, access_key_id=key_id,
    )
    if ok:
        return _confirmed(key_id, "deleted", started_at)
    if admin_api.is_not_found(err):
        # Positive 404 / NoSuchKey: the key is already gone. Idempotent
        # success - this is a tombstone-clearing signal, never a transient.
        return _confirmed(key_id, "already_absent", started_at)
    # Transport / 5xx / auth: do NOT certify the key as gone. The sweep
    # keeps the tombstone open and retries next tick.
    return _failure(key_id=key_id, stderr=err, started_at=started_at)


def _confirmed(key_id: str, outcome: str, started_at: float) -> JobOutcome:
    """The key is positively gone (deleted or already absent)."""
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"Key {key_id} {outcome}",
        extras={
            "key_id": key_id,
            "outcome": outcome,
            "confirmed_absent": True,
            "garage_stderr": "",
            "duration_seconds": _elapsed(started_at),
        },
    )


def _failure(*, key_id: str, stderr: str, started_at: float) -> JobOutcome:
    return JobOutcome(
        success=False,
        exit_code=-1,
        stdout="",
        stderr=stderr,
        failure_reason="key_delete_failed",
        extras={
            "key_id": key_id,
            "outcome": "transient_error",
            "confirmed_absent": False,
            "garage_stderr": stderr,
            "duration_seconds": _elapsed(started_at),
        },
    )


def _elapsed(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)
