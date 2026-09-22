"""Check Garage CLI version and RPC access before registering commands.

Return the first failure as GarageState.disabled_reason:
garage_version_unsupported, rpc_secret_unauthenticated, or garage_unreachable.
Checks are synchronous with subprocess timeouts. Storage type is not gated;
configure metadata durability in garage.toml at deployment."""

from __future__ import annotations

import logging
import subprocess
import tomllib

from stormpulse.garage.config import GarageConfig

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 15


def warn_if_s3_root_domain_set(config: GarageConfig) -> None:
    """Warn, without blocking startup, when S3 virtual-host addressing is enabled.

    Endpoints beneath s3_api.root_domain can be mistaken for bucket names, causing
    NoSuchBucket errors. Path-only stacks should leave root_domain unset.
    The website checks endpoint collisions at request time; see
    core/buckets-customer-truth.md."""
    try:
        with open(config.config_path, "rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return
    root_domain = (raw.get("s3_api") or {}).get("root_domain")
    if root_domain:
        logger.warning(
            "garage.toml [s3_api].root_domain = %r: S3 virtual-host "
            "addressing is ON. Ensure NO S3 endpoint host is a subdomain of "
            "it, or Garage parses the endpoint's own label as a bucket name "
            "and returns NoSuchBucket for every request (the root_domain "
            "trap). Path-only stacks should leave s3_api.root_domain unset. "
            "Verify the live S3 layer with s3-scripts/test_head_bucket.py.",
            root_domain,
        )


def check_garage_version(config: GarageConfig) -> str | None:
    """Garage CLI must report v2.x. Returns reason or None on pass."""
    cmd = [
        config.docker_binary,
        "exec",
        config.container_name,
        config.garage_binary,
        "--version",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            shell=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "garage_unreachable"
    if proc.returncode != 0:
        return "garage_unreachable"
    # Accept both "garage v2.x.y" and "v2.x.y" CLI output.
    out = (proc.stdout or "").strip().lower()
    if "v2." not in out:
        return "garage_version_unsupported"
    return None


def check_rpc_secret(config: GarageConfig) -> str | None:
    """Require garage status to exit zero; return None on success.

    Report auth-shaped stderr separately from generic unreachability."""
    cmd = [
        config.docker_binary,
        "exec",
        config.container_name,
        config.garage_binary,
        "status",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            shell=False,
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
    """Warn about root_domain, then check CLI version and RPC authentication.

    Return the first failure reason or None. Both checks use docker exec;
    container access failures return garage_unreachable."""
    warn_if_s3_root_domain_set(config)
    reason = check_garage_version(config)
    if reason:
        return reason
    reason = check_rpc_secret(config)
    if reason:
        return reason
    return None
