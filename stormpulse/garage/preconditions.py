"""Agent-start preconditions for the Garage Feature (ADR GARAGE-000).

Three checks run in order before the agent registers garage commands.
The first failure short-circuits and returns a named reason. The reason
is published on ``GarageState.disabled_reason`` so the dashboard sees a
clear cause rather than a missing-feature mystery.

Reasons (closed set):

- ``substrate_not_zfs`` — at least one of /var/lib/garage/{meta,data}
  is not a ZFS mount.
- ``garage_version_unsupported`` — the configured garage CLI does not
  report v2.x.
- ``rpc_secret_unauthenticated`` — ``garage status`` exited non-zero
  with an auth-shaped stderr.
- ``garage_unreachable`` — docker, the Garage container, or the CLI
  is not callable. Covers the FileNotFoundError, TimeoutExpired, and
  non-auth non-zero exit cases.

These checks are synchronous so the bootstrap code path can run them
without spinning an event loop. Each one wraps its subprocess in a
timeout and never raises; the worst case is a named reason.
"""

from __future__ import annotations

import subprocess

from stormpulse.config import GarageConfig

_TIMEOUT_SECONDS = 15
_REQUIRED_MOUNTS = ("/var/lib/garage/meta", "/var/lib/garage/data")


def check_substrate() -> str | None:
    """Both Garage paths must be ZFS mounts. Returns reason or None on pass."""
    for path in _REQUIRED_MOUNTS:
        try:
            proc = subprocess.run(
                ["findmnt", "-n", "-o", "FSTYPE", path],
                capture_output=True, text=True,
                timeout=_TIMEOUT_SECONDS, shell=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return "substrate_not_zfs"
        if proc.returncode != 0 or proc.stdout.strip() != "zfs":
            return "substrate_not_zfs"
    return None


def check_garage_version(config: GarageConfig) -> str | None:
    """Garage CLI must report v2.x. Returns reason or None on pass."""
    cmd = [
        config.docker_binary, "exec", config.container_name,
        config.garage_binary, "--version",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_TIMEOUT_SECONDS, shell=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "garage_unreachable"
    if proc.returncode != 0:
        return "garage_unreachable"
    # Garage v2 CLI prints either "garage v2.x.y" or "v2.x.y" depending
    # on subcommand. Substring check is sufficient for the major-version
    # gate this precondition enforces.
    out = (proc.stdout or "").strip().lower()
    if "v2." not in out:
        return "garage_version_unsupported"
    return None


def check_rpc_secret(config: GarageConfig) -> str | None:
    """`garage status` must exit 0. Returns reason or None on pass.

    Distinguishes auth failure (shape-matched in stderr) from generic
    unreachability so the operator gets a more specific reason when the
    container is up but the secret is wrong.
    """
    cmd = [
        config.docker_binary, "exec", config.container_name,
        config.garage_binary, "status",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=_TIMEOUT_SECONDS, shell=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "garage_unreachable"
    if proc.returncode != 0:
        stderr = (proc.stderr or "").lower()
        if (
            "secret" in stderr
            or "handshake" in stderr
            or "unauthorized" in stderr
            or "failed opening client secret box" in stderr
        ):
            return "rpc_secret_unauthenticated"
        return "garage_unreachable"
    return None


def run_preconditions(config: GarageConfig) -> str | None:
    """Run all three checks in order. Returns first failing reason or None.

    Order: substrate (host-level, cheap) → version (Garage CLI handshake)
    → rpc_secret (full auth round-trip). Failing early on substrate
    avoids spending a docker-exec budget on a host that was never going
    to work.
    """
    reason = check_substrate()
    if reason:
        return reason
    reason = check_garage_version(config)
    if reason:
        return reason
    reason = check_rpc_secret(config)
    if reason:
        return reason
    return None
