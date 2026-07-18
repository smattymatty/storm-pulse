"""Handler for ``garage_bucket_clear``.

Bulk-deletes every object in a bucket via the local Garage S3 endpoint.
Two modes, selected by the param shape:

- Customer-secret mode (the dashboard clear): the customer's admin secret
  rides in dispatch params, lives in process memory for the job's lifetime
  only, and is never persisted or logged.
- Credential-less mode (the purge clear): no secret in the
  envelope, just the 16-char ``bucket_id``. The agent self-mints a temporary
  key via the admin API, grants it on the bucket, attaches a throwaway local
  alias to it, clears via that alias, and destroys the key. The alias step is
  load-bearing: local aliases are key-scoped and S3 addresses buckets by
  name, never by id, so the minted key resolves no name for the bucket until
  it gets an alias of its own. The temporary secret is born, used, and
  destroyed here; it never leaves the node and never appears in logs or the
  command result.

The first ListObjectsV2 page validates credentials before any delete (it
is itself a signed request, so a wrong key raises S3AuthError there);
per-object errors from DeleteObjects fail the whole job (the bug class the
Django path got wrong - 200 OK with non-empty Errors silently treated as
success).

Failure reasons: ``auth_failed``, ``partial_failure``, ``os_error``, and in
credential-less mode ``admin_api_unconfigured``, ``purge_key_mint_failed``,
``purge_key_grant_failed``, ``purge_alias_failed``.
"""

from __future__ import annotations

import asyncio
import logging
import time

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.garage import admin_api
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.s3 import (
    GarageS3Client,
    S3AuthError,
    S3Error,
)

logger = logging.getLogger(__name__)


_BATCH_SIZE = 1000  # S3 DeleteObjects accepts at most 1000 keys per call.
_MAX_REPORTED_ERRORS = 10  # Trim the errors array on the wire to keep messages small.


def make_clear_bucket_handler(
    garage_config: GarageConfig,
    params: dict[str, str],
) -> JobHandler | None:
    """Build a JobHandler for ``garage_bucket_clear`` from runtime params.

    Two valid param shapes select the mode:

    - ``bucket_name`` + ``s3_endpoint`` + ``region`` + ``access_key_id`` +
      ``secret_access_key``: customer-secret clear.
    - ``bucket_id`` + ``s3_endpoint`` + ``region``, no credentials:
      credential-less purge clear.

    Returns None if neither shape is satisfied - the caller emits a
    structured no-handler failure rather than crashing.
    """
    has_key_id = bool(params.get("access_key_id"))
    has_secret = bool(params.get("secret_access_key"))
    if has_key_id != has_secret:
        logger.error(
            "garage_bucket_clear got half a credential pair; send both "
            "access_key_id and secret_access_key for a customer-secret "
            "clear, or neither plus bucket_id for a credential-less "
            "purge clear",
        )
        return None

    if not (params.get("s3_endpoint") and params.get("region")):
        logger.error(
            "garage_bucket_clear missing required params: %s",
            [k for k in ("s3_endpoint", "region") if not params.get(k)],
        )
        return None
    endpoint = params["s3_endpoint"]
    region = params["region"]

    if not has_key_id:
        if not params.get("bucket_id"):
            logger.error(
                "garage_bucket_clear without credentials requires bucket_id "
                "(the 16-char garage_bucket_id, never the local alias)",
            )
            return None
        bucket_id = params["bucket_id"]

        async def credential_less_handler(
            progress: ProgressCallback,
        ) -> JobOutcome:
            return await run_clear_bucket_credential_less(
                progress=progress,
                garage_config=garage_config,
                bucket_id=bucket_id,
                endpoint=endpoint,
                region=region,
            )

        return credential_less_handler

    if not params.get("bucket_name"):
        logger.error(
            "garage_bucket_clear missing required param: bucket_name",
        )
        return None

    bucket = params["bucket_name"]
    access_key = params["access_key_id"]
    secret_key = params["secret_access_key"]

    try:
        client = GarageS3Client(
            endpoint=endpoint,
            region=region,
            access_key=access_key,
            secret_key=secret_key,
        )
    except ValueError:
        logger.exception("Failed to construct GarageS3Client for clear_bucket")
        return None

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_clear_bucket(progress, client, bucket)

    return handler


async def run_clear_bucket_credential_less(
    *,
    progress: ProgressCallback,
    garage_config: GarageConfig,
    bucket_id: str,
    endpoint: str,
    region: str,
) -> JobOutcome:
    """Mint a temporary key, alias it onto the bucket, clear, destroy the key.

    The purge path: at purge time there is no customer
    session, so no customer secret can ride in the envelope. The key is
    minted first; everything after runs under a finally that deletes it, so
    no failure path leaks a live key. Deleting the key drops its local
    alias with it.
    """
    started_at = time.monotonic()
    admin_url, admin_token = garage_config.admin_url, garage_config.admin_token
    if not (admin_url and admin_token):
        # Fail loud: a credential-less clear never silently no-ops.
        return _credential_less_failure(
            failure_reason="admin_api_unconfigured",
            stderr=(
                "Garage admin API not configured (admin_url + admin_token); set "
                "[garage] admin_url and admin_token_file."
            ),
            started_at=started_at,
        )

    # Agent-side naming, like provisioning: never the customer's chosen name.
    purge_name = f"{bucket_id[:8]}-purge"

    await progress("starting", 0, None, "Minting temporary purge key")
    key_info, err = await asyncio.to_thread(
        admin_api.create_key,
        admin_url=admin_url, admin_token=admin_token, name=purge_name,
    )
    access_key_id = (key_info or {}).get("accessKeyId") or ""
    temp_secret = (key_info or {}).get("secretAccessKey") or ""
    if not (access_key_id and temp_secret):
        return _credential_less_failure(
            failure_reason="purge_key_mint_failed",
            stderr=(
                f"CreateKey failed for purge key {purge_name!r}: "
                f"{err or 'no key material in response'}"
            ),
            started_at=started_at,
        )

    try:
        outcome = await _clear_with_temp_key(
            progress=progress,
            admin_url=admin_url,
            admin_token=admin_token,
            bucket_id=bucket_id,
            endpoint=endpoint,
            region=region,
            access_key_id=access_key_id,
            temp_secret=temp_secret,
            purge_alias=purge_name,
            started_at=started_at,
        )
    finally:
        key_deleted, delete_err = await asyncio.to_thread(
            admin_api.delete_key,
            admin_url=admin_url, admin_token=admin_token,
            access_key_id=access_key_id,
        )
        if not key_deleted:
            # A leaked purge key holds read/write on a customer bucket. Logged
            # HERE so the trail survives even when the clear raised or was
            # cancelled and no outcome ever reaches the wire.
            logger.error(
                "purge key %s could not be deleted after clear of bucket %s: %s; "
                "delete it via the admin API (DeleteKey)",
                access_key_id, bucket_id[:16], delete_err,
            )

    outcome.extras["credential_less"] = True
    outcome.extras["purge_key_id"] = access_key_id
    if key_deleted:
        outcome.extras.setdefault("manual_cleanup_required", [])
    else:
        # Loud on the wire too, so the operator sees it in the result.
        outcome.extras["manual_cleanup_required"] = [
            {"type": "key", "id": access_key_id},
        ]
    return outcome


async def _clear_with_temp_key(
    *,
    progress: ProgressCallback,
    admin_url: str,
    admin_token: str,
    bucket_id: str,
    endpoint: str,
    region: str,
    access_key_id: str,
    temp_secret: str,
    purge_alias: str,
    started_at: float,
) -> JobOutcome:
    """Grant + alias the minted key, then run the ordinary clear via the alias."""
    ok, err = await asyncio.to_thread(
        admin_api.allow_bucket_key,
        admin_url=admin_url, admin_token=admin_token,
        bucket_ref=bucket_id, access_key_id=access_key_id,
        read=True, write=True,
    )
    if not ok:
        return _credential_less_failure(
            failure_reason="purge_key_grant_failed",
            stderr=f"AllowBucketKey failed for bucket {bucket_id[:16]}: {err}",
            started_at=started_at,
        )

    ok, err = await asyncio.to_thread(
        admin_api.add_bucket_alias_local,
        admin_url=admin_url, admin_token=admin_token,
        bucket_ref=bucket_id, access_key_id=access_key_id,
        local_alias=purge_alias,
    )
    if not ok:
        return _credential_less_failure(
            failure_reason="purge_alias_failed",
            stderr=(
                f"AddBucketAlias (local) failed for bucket {bucket_id[:16]}: "
                f"{err}. Without a key-scoped alias the minted key cannot "
                "address the bucket over S3."
            ),
            started_at=started_at,
        )

    try:
        client = GarageS3Client(
            endpoint=endpoint,
            region=region,
            access_key=access_key_id,
            secret_key=temp_secret,
        )
    except ValueError as exc:
        return _credential_less_failure(
            failure_reason="os_error",
            stderr=f"Failed to construct S3 client for purge clear: {exc}",
            started_at=started_at,
        )

    return await run_clear_bucket(progress, client, purge_alias)


def _credential_less_failure(
    *,
    failure_reason: str,
    stderr: str,
    started_at: float,
) -> JobOutcome:
    """A failure outcome shaped like run_clear_bucket's, pre-clear."""
    return JobOutcome(
        success=False,
        exit_code=-1,
        stderr=stderr,
        failure_reason=failure_reason,
        extras={
            "deleted_count": 0,
            "failed_count": 0,
            "errors": [],
            "duration_seconds": _elapsed(started_at),
            "error": stderr,
        },
    )


async def run_clear_bucket(
    progress: ProgressCallback,
    client: GarageS3Client,
    bucket: str,
) -> JobOutcome:
    """Clear all objects from ``bucket`` via the local Garage S3 endpoint.

    Tests inject a fake ``GarageS3Client``; production wires the real one.
    """
    started_at = time.monotonic()

    # ---- Phase 1: paginate the bucket to compute total ----
    # ListObjectsV2 is a signed, authenticated request, so the first page
    # IS the credential proof: a wrong key raises S3AuthError here, exactly
    # as a standalone HeadBucket pre-flight would have. Skipping that extra
    # pre-flight removes one authenticated round-trip per clear. The auth
    # branch must be caught BEFORE the generic S3Error (S3AuthError is a
    # subclass): downstream consumers distinguish a wrong-credential result
    # from an operational failure by failure_reason, so an auth failure
    # collapsed into os_error would silently lose that signal.
    await progress("starting", 0, None, "Listing objects")
    all_keys: list[str] = []
    continuation: str | None = None
    while True:
        try:
            page = await asyncio.to_thread(
                client.list_objects_v2,
                bucket,
                continuation,
            )
        except S3AuthError as exc:
            return JobOutcome(
                success=False,
                exit_code=-1,
                stderr=f"Authentication failed: {exc}",
                failure_reason="auth_failed",
                extras={
                    "deleted_count": 0,
                    "failed_count": 0,
                    "errors": [],
                    "duration_seconds": _elapsed(started_at),
                    "error": "Could not authenticate. Check your Admin secret key.",
                },
            )
        except S3Error as exc:
            return JobOutcome(
                success=False,
                exit_code=-1,
                stderr=f"List failed: {exc}",
                failure_reason="os_error",
                extras={
                    "deleted_count": 0,
                    "failed_count": 0,
                    "errors": [],
                    "duration_seconds": _elapsed(started_at),
                    "error": str(exc),
                },
            )
        all_keys.extend(o.key for o in page.contents)
        if not page.is_truncated:
            break
        continuation = page.next_continuation_token

    total = len(all_keys)
    if total == 0:
        # Nothing to delete - succeed with zero counts.
        return JobOutcome(
            success=True,
            exit_code=0,
            stdout="Bucket is already empty",
            extras={
                "deleted_count": 0,
                "failed_count": 0,
                "errors": [],
                "duration_seconds": _elapsed(started_at),
            },
        )

    # ---- Phase 2: delete in batches, emitting per-batch progress ----
    deleted_total = 0
    error_entries: list[dict[str, str]] = []
    for i in range(0, total, _BATCH_SIZE):
        batch = all_keys[i : i + _BATCH_SIZE]
        try:
            result = await asyncio.to_thread(
                client.delete_objects,
                bucket,
                batch,
            )
        except S3Error as exc:
            return JobOutcome(
                success=False,
                exit_code=-1,
                stderr=f"Delete batch failed: {exc}",
                failure_reason="os_error",
                extras={
                    "deleted_count": deleted_total,
                    "failed_count": total - deleted_total,
                    "errors": error_entries[:_MAX_REPORTED_ERRORS],
                    "duration_seconds": _elapsed(started_at),
                    "error": str(exc),
                },
            )
        deleted_total += len(result.deleted)
        for err in result.errors:
            error_entries.append(
                {"Key": err.key, "Code": err.code, "Message": err.message},
            )
        await progress(
            "running",
            deleted_total,
            total,
            f"Deleted {deleted_total} of {total}",
        )

    # ---- Phase 3: finalize and report ----
    await progress("finalizing", deleted_total, total, "Computing summary")

    if error_entries:
        # P1 contract: any per-object failure means the whole job failed.
        # The dashboard will leave the bucket counts untouched and let the
        # customer retry.
        return JobOutcome(
            success=False,
            exit_code=-1,
            stderr=f"{len(error_entries)} object(s) could not be deleted",
            failure_reason="partial_failure",
            extras={
                "deleted_count": deleted_total,
                "failed_count": len(error_entries),
                "errors": error_entries[:_MAX_REPORTED_ERRORS],
                "duration_seconds": _elapsed(started_at),
                "error": (
                    f"{len(error_entries)} of {total} objects could not be deleted. "
                    "The bucket was partially cleared; retry to finish."
                ),
            },
        )

    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"Cleared {deleted_total} object(s)",
        extras={
            "deleted_count": deleted_total,
            "failed_count": 0,
            "errors": [],
            "duration_seconds": _elapsed(started_at),
        },
    )


def _elapsed(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)
