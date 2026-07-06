"""Handler for ``rclone_restore_test``: prove data comes back from Storm.
Copy the first non-empty object to a scratch prefix in the same bucket,
verify with ``rclone check --download``, delete the prefix in a ``finally``."""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.rclone.config import RcloneConfig
from stormpulse.rclone.jobs import failure_outcome, remote_from_params
from stormpulse.rclone.runner import (
    DST_REMOTE,
    S3Remote,
    build_env,
    reason_for_exit,
    run_rclone,
    tail_capped,
)

logger = logging.getLogger(__name__)

# Reserved scratch prefix; sample selection skips it, cleanup purges it.
SCRATCH_PREFIX = ".storm-restore-test"

_STEP_TIMEOUT_SECONDS = 600
# rclone purge exit 3 = directory not found: nothing was written, clean.
_PURGE_CLEAN_EXITS = (0, 3)
# rclone check exit 1 = differences found: the restore proof failed.
_CHECK_MISMATCH_EXIT = 1

# rclone filters treat glob metacharacters specially; a key like
# "photo[1].jpg" must be escaped or the check filter matches nothing.
_GLOB_CHARS = re.compile(r"([\\*?\[\]{}])")


def _escape_filter(path: str) -> str:
    return _GLOB_CHARS.sub(r"\\\1", path)


def make_restore_test_handler(
    config: RcloneConfig,
    params: dict[str, str],
) -> JobHandler | None:
    """Build a JobHandler for ``rclone_restore_test``; None on missing params."""
    parsed = remote_from_params(params, "dst")
    if parsed is None:
        return None
    dest, bucket = parsed

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_restore_test(progress, config, dest, bucket)

    return handler


async def run_restore_test(
    progress: ProgressCallback,
    config: RcloneConfig,
    dest: S3Remote,
    bucket: str,
) -> JobOutcome:
    """Sample, round-trip, verify. Cleanup runs on every path."""
    started_at = time.monotonic()
    env = build_env(dst=dest)

    await progress("starting", 0, None, "Selecting sample object")
    sample = await _select_sample(config, env, bucket)
    if isinstance(sample, JobOutcome):
        return sample
    sample_path, sample_bytes = sample

    try:
        outcome = await _round_trip(
            progress, config, env, bucket, sample_path, sample_bytes, started_at
        )
    finally:
        cleanup_ok = await _purge_scratch(config, env, bucket)
    if not cleanup_ok:
        # Leftover scratch is customer-visible clutter in their own bucket.
        logger.error(
            "restore-test scratch prefix %s/%s could not be deleted; "
            "purge it manually",
            bucket,
            SCRATCH_PREFIX,
        )
        outcome.extras["manual_cleanup_required"] = [
            {"type": "prefix", "path": f"{bucket}/{SCRATCH_PREFIX}"},
        ]
    return outcome


async def _select_sample(
    config: RcloneConfig,
    env: dict[str, str],
    bucket: str,
) -> tuple[str, int] | JobOutcome:
    """First non-empty object outside the scratch prefix, or a failure
    outcome. Non-empty because a zero-byte marker proves nothing."""
    try:
        code, stdout, stderr = await run_rclone(
            config,
            "lsjson",
            f"{DST_REMOTE}:{bucket}",
            "--recursive",
            "--files-only",
            env=env,
            timeout=_STEP_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return failure_outcome("timeout", f"listing timed out after {_STEP_TIMEOUT_SECONDS}s")
    except OSError as exc:
        return failure_outcome("os_error", str(exc))
    if code != 0:
        return failure_outcome(reason_for_exit(code), tail_capped(stderr), exit_code=code)
    try:
        entries: list[dict[str, Any]] = json.loads(stdout)
    except ValueError:
        return failure_outcome("unparseable_output", "rclone lsjson returned unparseable JSON")
    for entry in entries:
        path = str(entry.get("Path", ""))
        size = int(entry.get("Size") or 0)
        if size > 0 and path and not path.startswith(f"{SCRATCH_PREFIX}/"):
            return (path, size)
    return failure_outcome(
        "no_sample_object",
        "Bucket has no non-empty object outside the scratch prefix; "
        "nothing to verify.",
    )


async def _round_trip(
    progress: ProgressCallback,
    config: RcloneConfig,
    env: dict[str, str],
    bucket: str,
    sample_path: str,
    sample_bytes: int,
    started_at: float,
) -> JobOutcome:
    """Copy the sample to the scratch prefix, then verify byte-for-byte."""
    await progress("running", 1, 3, "Copying sample to scratch prefix")
    try:
        code, _, stderr = await run_rclone(
            config,
            "copyto",
            f"{DST_REMOTE}:{bucket}/{sample_path}",
            f"{DST_REMOTE}:{bucket}/{SCRATCH_PREFIX}/{sample_path}",
            env=env,
            timeout=_STEP_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return failure_outcome("timeout", f"copy timed out after {_STEP_TIMEOUT_SECONDS}s")
    except OSError as exc:
        return failure_outcome("os_error", str(exc))
    if code != 0:
        return failure_outcome(reason_for_exit(code), tail_capped(stderr), exit_code=code)

    await progress("running", 2, 3, "Verifying restored copy against the Storm copy")
    try:
        code, _, stderr = await run_rclone(
            config,
            "check",
            f"{DST_REMOTE}:{bucket}",
            f"{DST_REMOTE}:{bucket}/{SCRATCH_PREFIX}",
            "--include",
            "/" + _escape_filter(sample_path),
            "--download",
            "--one-way",
            env=env,
            timeout=_STEP_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return failure_outcome("timeout", f"check timed out after {_STEP_TIMEOUT_SECONDS}s")
    except OSError as exc:
        return failure_outcome("os_error", str(exc))
    if code != 0:
        reason = (
            "restore_mismatch"
            if code == _CHECK_MISMATCH_EXIT
            else reason_for_exit(code)
        )
        return failure_outcome(reason, tail_capped(stderr), exit_code=code)

    await progress("finalizing", 3, 3, "Cleaning up scratch prefix")
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"Restored and verified {sample_path} ({sample_bytes} bytes)",
        extras={
            "sample_object": sample_path,
            "sample_bytes": sample_bytes,
            "duration_seconds": round(time.monotonic() - started_at, 3),
        },
    )


async def _purge_scratch(
    config: RcloneConfig,
    env: dict[str, str],
    bucket: str,
) -> bool:
    """Delete the scratch prefix; best-effort, never raises."""
    try:
        code, _, _ = await run_rclone(
            config,
            "purge",
            f"{DST_REMOTE}:{bucket}/{SCRATCH_PREFIX}",
            env=env,
            timeout=_STEP_TIMEOUT_SECONDS,
        )
    except (TimeoutError, OSError):
        return False
    return code in _PURGE_CLEAN_EXITS

