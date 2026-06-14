"""Handler for ``garage_set_account_key_create_bucket`` via the admin HTTP API.

The BUCKETS-012 count-backstop lever. The website dispatches this with an
account key's ``access_key_id`` and ``enable`` (``true``/``false``); the handler
POSTs ``UpdateKey`` to set or clear the key-level ``allow_create_bucket`` flag.
Off past the per-account bucket-count rail, back on when room opens.

The admin token is the node's, resolved from config and bound in by the factory
builder, never sent over the wire (ADR buckets/000). If the admin API is not
configured the handler fails loudly rather than leaving the flag in an unknown
state.
"""
from __future__ import annotations

import asyncio
import logging
import time

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.garage import admin_api

logger = logging.getLogger(__name__)


def make_set_account_key_capability_handler(
    params: dict[str, str], *, admin_url: str, admin_token: str,
) -> JobHandler | None:
    """Build a JobHandler for ``garage_set_account_key_create_bucket``.

    Required params: ``access_key_id``, ``enable`` (``true``/``false``).
    Returns None (-> structured no-handler failure) if a param is missing or
    invalid, or the admin API is not configured.
    """
    access_key_id = params.get("access_key_id", "")
    raw_enable = params.get("enable", "")
    if not access_key_id or raw_enable not in ("true", "false"):
        logger.error(
            "garage_set_account_key_create_bucket bad params: "
            "access_key_id=%r enable=%r",
            access_key_id, raw_enable,
        )
        return None
    if not admin_url or not admin_token:
        logger.error(
            "garage_set_account_key_create_bucket: Garage admin API not "
            "configured ([garage] admin_url + admin_token/admin_token_file)"
        )
        return None

    enable = raw_enable == "true"

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_set_account_key_capability(
            progress,
            admin_url=admin_url,
            admin_token=admin_token,
            access_key_id=access_key_id,
            enable=enable,
        )

    return handler


async def run_set_account_key_capability(
    progress: ProgressCallback,
    *,
    admin_url: str,
    admin_token: str,
    access_key_id: str,
    enable: bool,
) -> JobOutcome:
    """POST UpdateKey to set/clear the key's ``allow_create_bucket`` flag."""
    started_at = time.monotonic()
    verb = "Enabling" if enable else "Disabling"
    await progress("starting", 0, 1, f"{verb} create-bucket on key")

    ok, err = await asyncio.to_thread(
        admin_api.update_key,
        admin_url=admin_url,
        admin_token=admin_token,
        access_key_id=access_key_id,
        allow_create_bucket=enable,
    )
    if not ok:
        return JobOutcome(
            success=False,
            exit_code=-1,
            stderr=f"UpdateKey createBucket failed: {err}",
            failure_reason="os_error",
            extras={
                "duration_seconds": _elapsed(started_at),
                "error": err,
                "access_key_id": access_key_id,
                "enable": enable,
            },
        )

    await progress("finalizing", 1, 1, "Capability applied")
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"Set allow_create_bucket={enable} on {access_key_id}",
        extras={
            "access_key_id": access_key_id,
            "enable": enable,
            "duration_seconds": _elapsed(started_at),
        },
    )


def _elapsed(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)
