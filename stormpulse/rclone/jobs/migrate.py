"""Handler for ``rclone_migrate``: pull the source bucket into the Storm
bucket. Progress is aggregates only (per-object names are dropped on the
Runner); resume is re-dispatch, so exit 9 (nothing to transfer) is a success."""

from __future__ import annotations

import time
from typing import Any

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.rclone.config import RcloneConfig
from stormpulse.rclone.jobs import failure_outcome, remote_from_params
from stormpulse.rclone.runner import (
    DST_REMOTE,
    EXIT_NOTHING_TO_TRANSFER,
    SRC_REMOTE,
    S3Remote,
    build_env,
    reason_for_exit,
    run_rclone_streaming,
)

# Fixed stats cadence; a constant, not a knob.
_STATS_INTERVAL = "5s"


def make_migrate_handler(
    config: RcloneConfig,
    params: dict[str, str],
) -> JobHandler | None:
    """Build a JobHandler for ``rclone_migrate``; None on missing params."""
    parsed_src = remote_from_params(params, "src")
    parsed_dst = remote_from_params(params, "dst")
    if parsed_src is None or parsed_dst is None:
        return None
    source, src_bucket = parsed_src
    dest, dst_bucket = parsed_dst

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_migrate(
            progress, config, source, src_bucket, dest, dst_bucket
        )

    return handler


async def run_migrate(
    progress: ProgressCallback,
    config: RcloneConfig,
    source: S3Remote,
    src_bucket: str,
    dest: S3Remote,
    dst_bucket: str,
) -> JobOutcome:
    """``rclone copy`` source to destination with live aggregate progress."""
    started_at = time.monotonic()
    await progress("starting", 0, None, "Starting migration")
    env = build_env(src=source, dst=dest)
    last = {"bytes": 0, "objects": 0}

    async def on_stats(stats: dict[str, Any]) -> None:
        done_bytes = int(stats.get("bytes") or 0)
        total_bytes = stats.get("totalBytes")
        transfers = int(stats.get("transfers") or 0)
        total_transfers = int(stats.get("totalTransfers") or 0)
        eta = stats.get("eta")
        last["bytes"] = done_bytes
        last["objects"] = transfers
        message = f"{transfers} of {total_transfers} objects"
        if isinstance(eta, (int, float)):
            message += f", ETA {int(eta)}s"
        await progress(
            "running",
            done_bytes,
            int(total_bytes) if total_bytes else None,
            message,
        )

    try:
        code, stderr_tail = await run_rclone_streaming(
            config,
            "copy",
            f"{SRC_REMOTE}:{src_bucket}",
            f"{DST_REMOTE}:{dst_bucket}",
            "--use-json-log",
            "--stats",
            _STATS_INTERVAL,
            env=env,
            on_stats=on_stats,
        )
    except OSError as exc:
        return failure_outcome("os_error", str(exc))
    extras = {
        "bytes_transferred": last["bytes"],
        "objects_transferred": last["objects"],
        "duration_seconds": round(time.monotonic() - started_at, 3),
    }
    if code not in (0, EXIT_NOTHING_TO_TRANSFER):
        return failure_outcome(
            reason_for_exit(code), stderr_tail, exit_code=code, extras=extras
        )
    stdout = (
        "Nothing to transfer: destination already current"
        if code == EXIT_NOTHING_TO_TRANSFER
        else f"Migrated {last['objects']} objects, {last['bytes']} bytes"
    )
    return JobOutcome(success=True, exit_code=0, stdout=stdout, extras=extras)
