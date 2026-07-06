"""CommandSpecs for the rclone Integration: three jobs, group ``rclone``.
Credentials arrive as ``secret=True`` params and reach the subprocess as
env vars only; argv shows the operation and bucket paths, nothing else."""

from __future__ import annotations

from stormpulse.config import CommandSpec, ParamDef
from stormpulse.rclone.config import RcloneConfig
from stormpulse.rclone.jobs.estimate import make_estimate_handler
from stormpulse.rclone.jobs.migrate import make_migrate_handler
from stormpulse.rclone.jobs.restore_test import make_restore_test_handler

# https only: a plaintext endpoint would move customer objects unencrypted.
# Loosening is a deliberate future decision, never a silent acceptance.
_ENDPOINT_PATTERN = r"^https://[a-zA-Z0-9.-]+(:[0-9]+)?$"
_REGION_PATTERN = r"[a-zA-Z0-9_-]+"
_BUCKET_PATTERN = r"[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]"
_ACCESS_KEY_PATTERN = r"[A-Za-z0-9_-]+"


def _remote_params(side: str, label: str) -> dict[str, ParamDef]:
    """The five params naming one side of a transfer."""
    return {
        f"{side}_endpoint": ParamDef(
            placeholder=f"{side}_endpoint",
            default=None,
            pattern=_ENDPOINT_PATTERN,
            description=f"{label} S3 endpoint URL (no path/query)",
        ),
        f"{side}_region": ParamDef(
            placeholder=f"{side}_region",
            default=None,
            pattern=_REGION_PATTERN,
            description=f"{label} S3 region for SigV4 signing",
        ),
        f"{side}_bucket": ParamDef(
            placeholder=f"{side}_bucket",
            default=None,
            pattern=_BUCKET_PATTERN,
            description=f"{label} bucket name",
        ),
        f"{side}_access_key_id": ParamDef(
            placeholder=f"{side}_access_key_id",
            default=None,
            pattern=_ACCESS_KEY_PATTERN,
            description=f"{label} access key id",
        ),
        f"{side}_secret_access_key": ParamDef(
            placeholder=f"{side}_secret_access_key",
            default=None,
            max_bytes=256,
            secret=True,
            description=f"{label} secret access key (job-lifetime only)",
        ),
    }


def build_rclone_specs(config: RcloneConfig) -> dict[str, CommandSpec]:
    """The rclone command surface: estimate, migrate, restore test."""
    return {
        "rclone_estimate": CommandSpec(
            group="rclone",
            command=["rclone_estimate"],  # internal - handled by JobManager
            timeout=1800,
            description=(
                "Measure a source S3 bucket: total bytes and object count. "
                "Read-only; the capacity decision happens control-plane-side"
            ),
            mode="job",
            read_only=True,
            handler=lambda params: make_estimate_handler(config, params),
            params=_remote_params("src", "Source"),
        ),
        "rclone_migrate": CommandSpec(
            group="rclone",
            command=["rclone_migrate"],  # internal - handled by JobManager
            timeout=600,  # reference only; a job's total duration is unbounded
            description=(
                "One-time S3-to-S3 migration: pull every object from the "
                "source bucket into the destination bucket. Re-dispatch to "
                "resume; already-transferred objects are skipped"
            ),
            sensitive_output=True,  # credential-bearing long job
            mode="job",
            handler=lambda params: make_migrate_handler(config, params),
            params={
                **_remote_params("src", "Source"),
                **_remote_params("dst", "Destination"),
            },
        ),
        "rclone_restore_test": CommandSpec(
            group="rclone",
            command=["rclone_restore_test"],  # internal - handled by JobManager
            timeout=1800,
            description=(
                "Prove restore: copy the first non-empty object to a scratch "
                "prefix in the same bucket, verify it byte-for-byte against "
                "the stored copy, delete the scratch prefix"
            ),
            mode="job",
            handler=lambda params: make_restore_test_handler(config, params),
            params=_remote_params("dst", "Destination"),
        ),
    }
