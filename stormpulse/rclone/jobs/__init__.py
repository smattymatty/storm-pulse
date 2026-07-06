"""rclone job handlers, one module per job, plus shared param plumbing."""

from __future__ import annotations

import logging
from typing import Any

from stormpulse.commands.jobs import JobOutcome
from stormpulse.rclone.runner import S3Remote

logger = logging.getLogger(__name__)


def failure_outcome(
    reason: str,
    stderr: str,
    *,
    exit_code: int = -1,
    extras: dict[str, Any] | None = None,
) -> JobOutcome:
    """A failed JobOutcome with a named reason."""
    return JobOutcome(
        success=False,
        exit_code=exit_code,
        stderr=stderr,
        failure_reason=reason,
        extras=extras or {},
    )

_REMOTE_FIELDS = ("endpoint", "region", "bucket", "access_key_id", "secret_access_key")


def remote_from_params(
    params: dict[str, str],
    side: str,
) -> tuple[S3Remote, str] | None:
    """Build ``(remote, bucket)`` from the ``<side>_*`` params. Returns None
    (logging what is missing) so the caller emits a structured no-handler
    failure rather than crashing."""
    values: dict[str, str] = {}
    missing: list[str] = []
    for field in _REMOTE_FIELDS:
        value = params.get(f"{side}_{field}", "")
        if not value:
            missing.append(f"{side}_{field}")
        values[field] = value
    if missing:
        logger.error("rclone job missing required params: %s", missing)
        return None
    bucket = values.pop("bucket")
    return S3Remote(**values), bucket
