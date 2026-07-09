"""Handler for ``rclone_restore_test``: prove data comes back from Storm.

Builds a manifest (``rclone lsjson``), selects a segmented sample - the
largest object, the smallest non-empty object, and one object per
top-level folder, capped at a fixed total - copies each to a scratch
prefix in the same bucket, verifies the whole set with ``rclone check
--download``, and deletes the prefix in a ``finally``.

The segments cover the two failure modes that actually differ: the
largest object exercises size-related transfer handling (multipart,
large-object paths), and one object per folder catches structural bugs
(a dropped or mis-pathed folder), without the sample count scaling
unboundedly on a bucket with thousands of folders.
"""

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

# Ceiling on how many objects one restore test round-trips, regardless of
# bucket size. Largest + smallest always fit; folder samples fill the rest.
MAX_SAMPLES = 10

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

    await progress("starting", 0, None, "Building bucket manifest")
    listed = await _list_bucket(config, env, bucket)
    if isinstance(listed, JobOutcome):
        return listed
    samples = select_samples(listed)
    if not samples:
        return failure_outcome(
            "no_sample_object",
            "Bucket has no non-empty object outside the scratch prefix; "
            "nothing to verify.",
        )

    try:
        outcome = await _round_trip(
            progress, config, env, bucket, samples, started_at
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


async def _list_bucket(
    config: RcloneConfig,
    env: dict[str, str],
    bucket: str,
) -> list[dict[str, Any]] | JobOutcome:
    """The bucket's file listing, or a failure outcome."""
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
    return entries


def select_samples(entries: list[dict[str, Any]]) -> list[tuple[str, int, str]]:
    """Segmented sample selection over a parsed listing: ``(path, bytes,
    reason)`` triples, deduplicated, capped at ``MAX_SAMPLES``.

    Picks the largest object, the smallest non-empty object, then the
    smallest non-empty object in each top-level folder (cheapest probe
    that still proves the folder's objects exist and read back). Empty
    objects prove nothing and the scratch prefix is never sampled. Pure
    selection, no rclone - testable on its own.
    """
    eligible: list[tuple[str, int]] = []
    for entry in entries:
        path = str(entry.get("Path", ""))
        size = int(entry.get("Size") or 0)
        if size > 0 and path and not path.startswith(f"{SCRATCH_PREFIX}/"):
            eligible.append((path, size))
    if not eligible:
        return []

    picks: list[tuple[str, int, str]] = []
    chosen: set[str] = set()

    def pick(path: str, size: int, reason: str) -> None:
        # First reason wins: the largest object in a one-folder bucket is
        # reported as "largest", not re-listed as that folder's sample.
        if path in chosen:
            return
        chosen.add(path)
        picks.append((path, size, reason))

    largest = max(eligible, key=lambda e: (e[1], e[0]))
    pick(largest[0], largest[1], "largest")
    smallest = min(eligible, key=lambda e: (e[1], e[0]))
    pick(smallest[0], smallest[1], "smallest")

    by_folder: dict[str, tuple[str, int]] = {}
    for path, size in eligible:
        if "/" not in path:
            continue  # root objects have no folder; largest/smallest cover them
        folder = path.split("/", 1)[0]
        best = by_folder.get(folder)
        if best is None or (size, path) < (best[1], best[0]):
            by_folder[folder] = (path, size)
    for folder in sorted(by_folder):
        if len(picks) >= MAX_SAMPLES:
            break
        path, size = by_folder[folder]
        pick(path, size, "prefix_sample")

    return picks


async def _round_trip(
    progress: ProgressCallback,
    config: RcloneConfig,
    env: dict[str, str],
    bucket: str,
    samples: list[tuple[str, int, str]],
    started_at: float,
) -> JobOutcome:
    """Copy every sample to the scratch prefix, then verify the whole set
    byte-for-byte with one ``rclone check --download``."""
    total_steps = len(samples) + 2

    for i, (path, _size, _reason) in enumerate(samples, start=1):
        await progress(
            "running", i, total_steps,
            f"Copying sample {i}/{len(samples)} to scratch prefix",
        )
        try:
            code, _, stderr = await run_rclone(
                config,
                "copyto",
                f"{DST_REMOTE}:{bucket}/{path}",
                f"{DST_REMOTE}:{bucket}/{SCRATCH_PREFIX}/{path}",
                env=env,
                timeout=_STEP_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            return failure_outcome("timeout", f"copy timed out after {_STEP_TIMEOUT_SECONDS}s")
        except OSError as exc:
            return failure_outcome("os_error", str(exc))
        if code != 0:
            return failure_outcome(reason_for_exit(code), tail_capped(stderr), exit_code=code)

    await progress(
        "running", len(samples) + 1, total_steps,
        "Verifying restored copies against the Storm copies",
    )
    include_filters: list[str] = []
    for path, _size, _reason in samples:
        include_filters.extend(("--include", "/" + _escape_filter(path)))
    try:
        code, _, stderr = await run_rclone(
            config,
            "check",
            f"{DST_REMOTE}:{bucket}",
            f"{DST_REMOTE}:{bucket}/{SCRATCH_PREFIX}",
            *include_filters,
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

    await progress("finalizing", total_steps, total_steps, "Cleaning up scratch prefix")
    total_bytes = sum(size for _path, size, _reason in samples)
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=(
            f"Restored and verified {len(samples)} object(s), "
            f"{total_bytes} bytes"
        ),
        extras={
            # The full receipt: which objects, how big, and why each was
            # selected - so the dashboard can state exactly what this test
            # checked, never a bare pass/fail.
            "samples": [
                {"key": path, "bytes": size, "reason": reason}
                for path, size, reason in samples
            ],
            # Older dashboards read the single-object shape; keep it
            # pointed at the first sample so they stay correct.
            "sample_object": samples[0][0],
            "sample_bytes": samples[0][1],
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
