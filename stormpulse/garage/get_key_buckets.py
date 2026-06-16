"""Handler for ``garage_get_key_buckets``.

Read-only: return the buckets an account key owns, by reading the key over the
admin API (``GetKeyInfo``). Storm does not store the account-key -> bucket link
(ownership lives in Garage), so the dashboard's per-key bucket list and its
revoke at-risk/safe split come from this live read. No mutation, no
confirmation; an already-gone key (404) returns an empty list.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.config import GarageConfig
from stormpulse.garage import admin_api

logger = logging.getLogger(__name__)


def make_get_key_buckets_handler(
    garage_config: GarageConfig,
    params: dict[str, str],
) -> JobHandler | None:
    """Build a JobHandler from runtime params. Required: ``key_id``."""
    if not params.get("key_id"):
        logger.error("garage_get_key_buckets missing required param: key_id")
        return None
    key_id = params["key_id"]

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_get_key_buckets(
            progress=progress, garage_config=garage_config, key_id=key_id,
        )

    return handler


async def run_get_key_buckets(
    progress: ProgressCallback,
    garage_config: GarageConfig,
    key_id: str,
) -> JobOutcome:
    """Return ``[{id, alias}]`` for every bucket the key owns."""
    started_at = time.monotonic()
    admin_url, admin_token = garage_config.admin_url, garage_config.admin_token
    if not (admin_url and admin_token):
        return _failure(
            failure_reason="admin_api_unconfigured",
            key_id=key_id,
            stderr=(
                "Garage admin API not configured (admin_url + admin_token); set "
                "[garage] admin_url and admin_token_file."
            ),
            started_at=started_at,
        )

    await progress("starting", 0, 1, "Reading key buckets")
    kinfo, err = admin_api.get_key_info(
        admin_url=admin_url, admin_token=admin_token, access_key_id=key_id,
    )
    if kinfo is None:
        if admin_api.is_not_found(err):
            return _success(key_id, owned=[], started_at=started_at)
        return _failure(
            failure_reason="key_read_failed",
            key_id=key_id, stderr=err, started_at=started_at,
        )

    owned: list[dict[str, str]] = []
    for entry in kinfo.get("buckets") or []:
        full_id = entry.get("id") or ""
        perms = entry.get("permissions") or {}
        if not (full_id and perms.get("owner")):
            continue
        aliases = entry.get("localAliases") or []
        owned.append({"id": full_id, "alias": aliases[0] if aliases else ""})
    return _success(key_id, owned=owned, started_at=started_at)


def _success(
    key_id: str, *, owned: list[dict[str, str]], started_at: float,
) -> JobOutcome:
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"Key {key_id} owns {len(owned)} bucket(s)",
        extras={
            "key_id": key_id,
            "owned_buckets": owned,
            "garage_stderr": "",
            "duration_seconds": _elapsed(started_at),
        },
    )


def _failure(
    *, failure_reason: str, key_id: str, stderr: str, started_at: float,
) -> JobOutcome:
    return JobOutcome(
        success=False,
        exit_code=-1,
        stdout="",
        stderr=stderr,
        failure_reason=failure_reason,
        extras={
            "key_id": key_id,
            "owned_buckets": [],
            "garage_stderr": stderr,
            "duration_seconds": _elapsed(started_at),
        },
    )


def _elapsed(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)
