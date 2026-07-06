"""Handler for ``rclone_estimate``: measure the source bucket, decide nothing.
Bytes and object count ride the JobOutcome extras; the quota gate that acts
on them is control-plane-side."""

from __future__ import annotations

import json
import time

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.rclone.config import RcloneConfig
from stormpulse.rclone.jobs import failure_outcome, remote_from_params
from stormpulse.rclone.runner import (
    SRC_REMOTE,
    S3Remote,
    build_env,
    reason_for_exit,
    run_rclone,
    tail_capped,
)

# Listing a large source bucket is slow, not hung.
_ESTIMATE_TIMEOUT_SECONDS = 1800


def make_estimate_handler(
    config: RcloneConfig,
    params: dict[str, str],
) -> JobHandler | None:
    """Build a JobHandler for ``rclone_estimate``; None on missing params."""
    parsed = remote_from_params(params, "src")
    if parsed is None:
        return None
    source, bucket = parsed

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_estimate(progress, config, source, bucket)

    return handler


async def run_estimate(
    progress: ProgressCallback,
    config: RcloneConfig,
    source: S3Remote,
    bucket: str,
) -> JobOutcome:
    """``rclone size --json`` on the source bucket."""
    started_at = time.monotonic()
    await progress("starting", 0, None, "Measuring source bucket")
    env = build_env(src=source)
    try:
        code, stdout, stderr = await run_rclone(
            config,
            "size",
            f"{SRC_REMOTE}:{bucket}",
            "--json",
            env=env,
            timeout=_ESTIMATE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return failure_outcome(
            "timeout", f"rclone size timed out after {_ESTIMATE_TIMEOUT_SECONDS}s"
        )
    except OSError as exc:
        return failure_outcome("os_error", str(exc))
    if code != 0:
        return failure_outcome(
            reason_for_exit(code), tail_capped(stderr), exit_code=code
        )
    try:
        data = json.loads(stdout)
        total_bytes = int(data["bytes"])
        objects = int(data["count"])
    except (ValueError, KeyError, TypeError):
        return failure_outcome(
            "unparseable_output", "rclone size returned unparseable JSON",
            exit_code=code,
        )
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"{objects} objects, {total_bytes} bytes",
        extras={
            "bytes": total_bytes,
            "objects": objects,
            "duration_seconds": round(time.monotonic() - started_at, 3),
        },
    )
