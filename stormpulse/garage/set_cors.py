"""Handler for ``garage_bucket_set_cors``.

Issues one ``PutBucketCors`` with a platform-default rule (methods, headers,
expose-headers, max-age hardcoded; only origins varies). The customer's
admin secret rides in dispatch params, lives in process memory for the job's
lifetime, and is never persisted or logged.

Pulse params are string-keyed-string-valued, so ``origins`` is JSON-encoded
on the wire and decoded here. Decode failure or wrong shape returns ``None``
from the factory - same disposition as a missing required param.

Failure reasons mirror ``clear_bucket``: ``auth_failed``, ``os_error``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.garage.s3 import (
    CorsRule,
    GarageS3Client,
    S3AuthError,
    S3Error,
)

logger = logging.getLogger(__name__)


# Platform-wide CORS rule shape. Only origins varies per call.
ALLOWED_METHODS: list[str] = ["GET", "PUT", "HEAD", "POST"]
ALLOWED_HEADERS: list[str] = [
    "authorization",
    "x-amz-date",
    "x-amz-content-sha256",
    "content-type",
    "content-length",
]
EXPOSE_HEADERS: list[str] = ["ETag"]
MAX_AGE_SECONDS: int = 3000


def make_set_cors_handler(params: dict[str, str]) -> JobHandler | None:
    """Build a JobHandler for ``garage_bucket_set_cors`` from runtime params.

    Returns None if a required param is missing, ``origins`` fails to
    decode as a non-empty ``list[str]``, or the S3 endpoint is malformed
    - the caller emits a structured no-handler failure rather than crashing.
    """
    required = (
        "bucket_name",
        "s3_endpoint",
        "region",
        "access_key_id",
        "secret_access_key",
        "origins",
    )
    if not all(params.get(k) for k in required):
        logger.error(
            "garage_bucket_set_cors missing required params: %s",
            [k for k in required if not params.get(k)],
        )
        return None

    bucket = params["bucket_name"]
    endpoint = params["s3_endpoint"]
    region = params["region"]
    access_key = params["access_key_id"]
    secret_key = params["secret_access_key"]

    origins = _decode_origins(params["origins"])
    if origins is None:
        return None

    try:
        client = GarageS3Client(
            endpoint=endpoint,
            region=region,
            access_key=access_key,
            secret_key=secret_key,
        )
    except ValueError:
        logger.exception("Failed to construct GarageS3Client for set_cors")
        return None

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_set_cors(progress, client, bucket, origins)

    return handler


def _decode_origins(raw: str) -> list[str] | None:
    """Decode the ``origins`` JSON-string param.

    Returns the list on success, ``None`` on any failure. Logs the failure
    reason without echoing the raw value (it isn't a secret, but the log
    line is more useful as a count + type than as user input).
    """
    try:
        decoded: Any = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("garage_bucket_set_cors: origins is not valid JSON")
        return None
    if not isinstance(decoded, list) or not decoded:
        logger.error(
            "garage_bucket_set_cors: origins must be a non-empty list, got %s",
            type(decoded).__name__,
        )
        return None
    if not all(isinstance(o, str) and o for o in decoded):
        logger.error(
            "garage_bucket_set_cors: every origin must be a non-empty string",
        )
        return None
    return decoded


async def run_set_cors(
    progress: ProgressCallback,
    client: GarageS3Client,
    bucket: str,
    origins: list[str],
) -> JobOutcome:
    """Apply the platform-default CORS rule to ``bucket`` with ``origins``.

    Tests inject a fake ``GarageS3Client``; production wires the real one.
    """
    started_at = time.monotonic()
    rule = CorsRule(
        allowed_origins=origins,
        allowed_methods=ALLOWED_METHODS,
        allowed_headers=ALLOWED_HEADERS,
        expose_headers=EXPOSE_HEADERS,
        max_age_seconds=MAX_AGE_SECONDS,
    )

    await progress("starting", 0, 1, "Applying CORS rule")
    try:
        await asyncio.to_thread(client.put_bucket_cors, bucket, rule)
    except S3AuthError as exc:
        return JobOutcome(
            success=False,
            exit_code=-1,
            stderr=f"Authentication failed: {exc}",
            failure_reason="auth_failed",
            extras={
                "duration_seconds": _elapsed(started_at),
                "error": "Could not authenticate. Check your Admin secret key.",
            },
        )
    except S3Error as exc:
        return JobOutcome(
            success=False,
            exit_code=-1,
            stderr=f"PutBucketCors failed: {exc}",
            failure_reason="os_error",
            extras={
                "duration_seconds": _elapsed(started_at),
                "error": str(exc),
            },
        )

    await progress("finalizing", 1, 1, "CORS rule applied")
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"Applied CORS rule with {len(origins)} origin(s)",
        extras={
            "origins": origins,
            "allowed_methods": ALLOWED_METHODS,
            "allowed_headers": ALLOWED_HEADERS,
            "expose_headers": EXPOSE_HEADERS,
            "max_age_seconds": MAX_AGE_SECONDS,
            "duration_seconds": _elapsed(started_at),
        },
    )


def _elapsed(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)
