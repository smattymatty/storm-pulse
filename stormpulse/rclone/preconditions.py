"""Agent-start precondition: the configured rclone binary must execute.
Failure publishes the named reason ``rclone_unavailable`` as the
Integration's disabled_reason (CORE-005 decision 5). Never raises."""

from __future__ import annotations

import subprocess

from stormpulse.rclone.config import RcloneConfig

_TIMEOUT_SECONDS = 15


def check_rclone_binary(config: RcloneConfig) -> str | None:
    """``rclone version`` must exit 0. Returns reason or None on pass."""
    try:
        proc = subprocess.run(
            [config.binary_path, "version"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            shell=False,
        )
    except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired):
        return "rclone_unavailable"
    if proc.returncode != 0:
        return "rclone_unavailable"
    return None


def run_preconditions(config: RcloneConfig) -> str | None:
    """Run all checks in order. Returns first failing reason or None."""
    return check_rclone_binary(config)
