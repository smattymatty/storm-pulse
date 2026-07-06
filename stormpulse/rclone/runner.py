"""Subprocess helpers for invoking rclone with env-var remotes.
``RCLONE_CONFIG=/dev/null`` makes the env-var remotes the only remotes;
credentials never appear in argv or on disk."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from stormpulse.rclone.config import RcloneConfig

logger = logging.getLogger(__name__)

SRC_REMOTE = "SRC"
DST_REMOTE = "DST"

# The subprocess env is built from scratch; only these pass through.
_ENV_PASSTHROUGH = ("PATH", "HOME", "TMPDIR")

# SIGTERM grace before SIGKILL: a hard kill orphans in-flight multipart
# uploads, whose parts persist and count against destination usage.
_TERMINATE_GRACE_SECONDS = 10

# Failure stderr rides job_result events as ``error``; only this many
# trailing bytes leave the Runner.
MAX_STDERR_TAIL_BYTES = 4096

# Stats lines naming many in-flight transfers can exceed asyncio's default
# 64 KiB stream limit; a burst line must not kill a multi-hour job.
_STREAM_LIMIT_BYTES = 1024 * 1024

# rclone's documented exit codes, as named reasons.
_EXIT_REASONS = {
    1: "usage_error",
    2: "rclone_error",
    3: "path_not_found",
    4: "path_not_found",
    5: "retryable_error",
    6: "partial_failure",
    7: "fatal_error",
    8: "transfer_limit_exceeded",
    10: "duration_limit_exceeded",
}

# "Operation successful, but no files transferred": the re-dispatch resume
# case. A success, not a failure.
EXIT_NOTHING_TO_TRANSFER = 9


def reason_for_exit(code: int) -> str:
    """Named failure reason for an rclone exit code."""
    return _EXIT_REASONS.get(code, f"rclone_exit_{code}")


def tail_capped(text: str) -> str:
    """The last MAX_STDERR_TAIL_BYTES bytes of ``text``."""
    raw = text.encode("utf-8")
    if len(raw) <= MAX_STDERR_TAIL_BYTES:
        return text
    return raw[-MAX_STDERR_TAIL_BYTES:].decode("utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class S3Remote:
    """One side of a transfer: an S3 endpoint plus its credentials."""

    endpoint: str
    region: str
    access_key_id: str
    secret_access_key: str


def build_env(
    *,
    src: S3Remote | None = None,
    dst: S3Remote | None = None,
) -> dict[str, str]:
    """Minimal env plus the remote definitions, built per job and dropped
    with it. Never a copy of the agent's env: nothing secret in it may reach
    the subprocess, and a stray RCLONE_* var must not reconfigure rclone."""
    env = {key: os.environ[key] for key in _ENV_PASSTHROUGH if key in os.environ}
    env["RCLONE_CONFIG"] = "/dev/null"
    for name, remote in ((SRC_REMOTE, src), (DST_REMOTE, dst)):
        if remote is None:
            continue
        prefix = f"RCLONE_CONFIG_{name}_"
        env[prefix + "TYPE"] = "s3"
        env[prefix + "PROVIDER"] = "Other"
        env[prefix + "ENDPOINT"] = remote.endpoint
        env[prefix + "REGION"] = remote.region
        env[prefix + "ACCESS_KEY_ID"] = remote.access_key_id
        env[prefix + "SECRET_ACCESS_KEY"] = remote.secret_access_key
    return env


async def run_rclone(
    config: RcloneConfig,
    *args: str,
    env: dict[str, str],
    timeout: float,
) -> tuple[int, str, str]:
    """Run rclone, capture output, return ``(returncode, stdout, stderr)``.
    On timeout the subprocess is killed and ``TimeoutError`` propagates."""
    proc = await asyncio.create_subprocess_exec(
        config.binary_path,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
    except TimeoutError:
        await stop_process(proc)
        raise
    return (
        proc.returncode if proc.returncode is not None else -1,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


StatsCallback = Callable[[dict[str, Any]], Awaitable[None]]
"""Receives each ``--use-json-log`` stats object. Forward aggregates only,
never the per-object names inside it."""


async def run_rclone_streaming(
    config: RcloneConfig,
    *args: str,
    env: dict[str, str],
    on_stats: StatsCallback,
) -> tuple[int, str]:
    """Run rclone, parsing ``--use-json-log`` stderr live; stats lines go to
    ``on_stats``, everything else feeds the byte-capped tail returned as
    ``(returncode, stderr_tail)``. No overall timeout: resume is re-dispatch."""
    proc = await asyncio.create_subprocess_exec(
        config.binary_path,
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        limit=_STREAM_LIMIT_BYTES,
    )
    tail: deque[str] = deque()
    tail_bytes = 0
    assert proc.stderr is not None
    try:
        while True:
            raw = await proc.stderr.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            stats = _parse_stats_line(line)
            if stats is not None:
                try:
                    await on_stats(stats)
                except Exception:
                    # Progress forwarding must never abort the transfer.
                    logger.warning(
                        "stats forwarding failed; transfer continues",
                        exc_info=True,
                    )
                continue
            tail.append(line)
            tail_bytes += len(line) + 1
            while tail_bytes > MAX_STDERR_TAIL_BYTES and tail:
                tail_bytes -= len(tail.popleft()) + 1
        code = await proc.wait()
    finally:
        # Job cancellation (agent reconnect) must not orphan the subprocess.
        if proc.returncode is None:
            try:
                await stop_process(proc)
            except asyncio.CancelledError:
                proc.kill()
                raise
    return (code, "\n".join(tail))


async def stop_process(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM, grace window, then SIGKILL, so rclone gets a chance to
    abort in-flight multipart uploads before dying."""
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_GRACE_SECONDS)
    except TimeoutError:
        proc.kill()
        await proc.wait()


def _parse_stats_line(line: str) -> dict[str, Any] | None:
    """The ``stats`` object of a JSON log line, or None for any other line."""
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    stats = obj.get("stats") if isinstance(obj, dict) else None
    return stats if isinstance(stats, dict) else None
