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
is itself a signed request, so a wrong key raises S3AuthError there). The
clear itself is a self-converging drain loop (see ``run_clear_bucket``): an
empty list is the proof of success, so a transient delete failure is retried
rather than failing the whole job.

Failure reasons: ``auth_failed``, ``clear_stalled`` (drained what it could,
the rest awaits a retry), and in credential-less mode
``admin_api_unconfigured``, ``purge_key_mint_failed``,
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


_DELETE_BATCH_SIZE = 250  # Keep each DeleteObjects well under the 30s socket read.
_MAX_REPORTED_ERRORS = 10  # Trim the errors array on the wire to keep messages small.
_MAX_NO_PROGRESS_ROUNDS = 3  # Consecutive rounds freeing zero objects before giving up.
_MAX_WALL_SECONDS = 600  # Hard backstop: a clear never runs longer than this.


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
            "bytes_freed": 0,
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
    """Drain every object from ``bucket`` via the local Garage S3 endpoint.

    A self-converging loop, not a two-phase list-then-delete: each round
    lists one page from the front, deletes it in small batches, and re-lists.
    An empty list IS the proof the bucket is clear - the fail-safe KEEP rule
    holds structurally, since a key never positively deleted simply reappears
    next round and gets retried. This absorbs the "Garage deleted it but the
    response timed out" case (the deleted keys don't come back) and needs no
    up-front total, so O(one page) memory regardless of bucket size.

    Progress counts UP: objects deleted and bytes freed, both summed from
    keys Garage positively confirmed. The dashboard draws an approximate bar
    from the bucket size it already knows; the numbers here are exact.

    Termination is guaranteed two ways: ``_MAX_NO_PROGRESS_ROUNDS`` consecutive
    rounds that free zero objects (a stuck object, or Garage unreachable), and
    a ``_MAX_WALL_SECONDS`` backstop. Either bails with the freed-so-far count,
    leaving the rest for a later retry - re-dispatching a clear is idempotent
    by design (an empty list is the only success proof), so resuming is safe.

    Tests inject a fake ``GarageS3Client``; production wires the real one.
    """
    started_at = time.monotonic()
    deleted_total = 0
    bytes_freed = 0
    no_progress_rounds = 0
    last_errors: list[dict[str, str]] = []
    last_transport: str | None = None

    # Leave the "0 objects" state immediately: a large bucket's first list can
    # take a beat, and the customer must see the modal is alive before then.
    await progress("running", 0, None, "Clearing", bytes_freed=0)

    while True:
        if _elapsed(started_at) > _MAX_WALL_SECONDS:
            return _clear_stalled(
                deleted_total,
                bytes_freed,
                last_errors,
                last_transport or f"clear exceeded {_MAX_WALL_SECONDS}s",
                started_at,
            )

        # ListObjectsV2 is a signed request, so the first list doubles as the
        # credential proof: a wrong key raises S3AuthError here. The auth branch
        # must precede the generic S3Error (it is a subclass) so a wrong secret
        # reports auth_failed, never a stalled-clear give-up.
        try:
            page = await asyncio.to_thread(client.list_objects_v2, bucket)
        except S3AuthError as exc:
            return _auth_failure(exc, started_at)
        except S3Error as exc:
            no_progress_rounds += 1
            last_transport = str(exc)
            if no_progress_rounds >= _MAX_NO_PROGRESS_ROUNDS:
                return _clear_stalled(
                    deleted_total, bytes_freed, last_errors, last_transport, started_at
                )
            continue

        if not page.contents:
            break  # Empty list: the bucket is drained. This is the success proof.

        size_by_key = {o.key: o.size for o in page.contents}
        keys = list(size_by_key)
        deleted_before = deleted_total
        last_errors = []

        for i in range(0, len(keys), _DELETE_BATCH_SIZE):
            batch = keys[i : i + _DELETE_BATCH_SIZE]
            try:
                result = await asyncio.to_thread(client.delete_objects, bucket, batch)
            except S3Error as exc:
                # Transient: bail this page and re-list. Garage may have deleted
                # some of the batch server-side; those keys won't come back, so
                # the loop still makes forward progress.
                last_transport = str(exc)
                break
            deleted_total += len(result.deleted)
            bytes_freed += sum(size_by_key.get(k, 0) for k in result.deleted)
            last_errors.extend(
                {"Key": err.key, "Code": err.code, "Message": err.message}
                for err in result.errors
            )
            await progress(
                "running",
                deleted_total,
                None,
                f"Deleted {deleted_total}",
                bytes_freed=bytes_freed,
            )

        if deleted_total == deleted_before:
            no_progress_rounds += 1
            if no_progress_rounds >= _MAX_NO_PROGRESS_ROUNDS:
                return _clear_stalled(
                    deleted_total,
                    bytes_freed,
                    last_errors,
                    last_transport or "no objects could be deleted",
                    started_at,
                )
        else:
            no_progress_rounds = 0

    await progress(
        "finalizing", deleted_total, None, "Done", bytes_freed=bytes_freed
    )
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"Cleared {deleted_total} object(s)",
        extras={
            "deleted_count": deleted_total,
            "bytes_freed": bytes_freed,
            "failed_count": 0,
            "errors": [],
            "duration_seconds": _elapsed(started_at),
        },
    )


def _auth_failure(exc: S3Error, started_at: float) -> JobOutcome:
    """Wrong-credential outcome. Distinct reason so consumers don't retry it."""
    return JobOutcome(
        success=False,
        exit_code=-1,
        stderr=f"Authentication failed: {exc}",
        failure_reason="auth_failed",
        extras={
            "deleted_count": 0,
            "bytes_freed": 0,
            "failed_count": 0,
            "errors": [],
            "duration_seconds": _elapsed(started_at),
            "error": "Could not authenticate. Check your Admin secret key.",
        },
    )


def _clear_stalled(
    deleted_total: int,
    bytes_freed: int,
    errors: list[dict[str, str]],
    detail: str,
    started_at: float,
) -> JobOutcome:
    """Gave-up outcome: freed what it could, the rest awaits a retry.

    Not a partial-success accounting claim - the bucket is simply not yet
    empty. Re-dispatch (dashboard retry or purge tick) resumes from here.
    """
    return JobOutcome(
        success=False,
        exit_code=-1,
        stderr=f"Clear stalled after freeing {deleted_total} object(s): {detail}",
        failure_reason="clear_stalled",
        extras={
            "deleted_count": deleted_total,
            "bytes_freed": bytes_freed,
            "failed_count": len(errors),
            "errors": errors[:_MAX_REPORTED_ERRORS],
            "duration_seconds": _elapsed(started_at),
            "error": (
                f"Cleared {deleted_total} object(s) but could not finish "
                f"({detail}). Retry to continue."
            ),
        },
    )


def _elapsed(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)
