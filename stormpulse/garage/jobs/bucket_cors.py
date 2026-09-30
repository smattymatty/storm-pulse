"""Handlers for ``garage_bucket_cors_get`` and ``garage_bucket_cors_set``.

Per-bucket CORS rules via the Garage admin API, in Garage's own JSON shape (the
S3 XML names). The set is a compare-and-swap: the caller sends the rules it
loaded as ``expected_rules``; live rules that differ are refused with
``cors_rules_stale`` and returned, so two editors never overwrite each other.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.garage import admin_api

logger = logging.getLogger(__name__)

CorsRules = list[dict[str, Any]]

_LIST_KEYS = ("AllowedOrigin", "AllowedMethod", "AllowedHeader", "ExposeHeader")
_SCALAR_KEYS = ("ID", "MaxAgeSeconds")
_RULE_KEYS = frozenset(_LIST_KEYS + _SCALAR_KEYS)
_BUCKET_ID = "bucket_id"


def cors_rules_shape_problem(rules: object) -> str | None:
    """Why ``rules`` is not a list of rule objects on the six known keys, or None.
    The read side of the compare-and-swap refuses on this instead of guessing:
    a shape this module was not written for must never read as "no rules"."""
    if not isinstance(rules, list):
        return f"is {type(rules).__name__}, expected a list of rule objects"
    for i, rule in enumerate(rules):
        if not isinstance(rule, dict):
            return f"[{i}] is {type(rule).__name__}, expected a rule object"
        unknown = sorted(set(rule) - _RULE_KEYS)
        if unknown:
            return f"[{i}] carries unknown keys {unknown}"
    return None


def normalize_cors_rules(rules: CorsRules) -> CorsRules:
    """Garage's read-back form: every list present (``null``/absent -> ``[]``),
    optional scalars only when set. Applied to both sides of the stale compare
    and to the payload written, so what is sent equals what reads back."""
    out: CorsRules = []
    for rule in rules:
        clean: dict[str, Any] = {
            k: rule[k] for k in _SCALAR_KEYS if rule.get(k) is not None
        }
        clean |= {k: list(rule.get(k) or []) for k in _LIST_KEYS}
        out.append(clean)
    return out


def make_bucket_cors_get_handler(
    params: dict[str, str],
    *,
    admin_url: str,
    admin_token: str,
) -> JobHandler | None:
    """Build the read handler. Required: ``bucket_id``; admin API configured."""
    bucket_id = params.get(_BUCKET_ID, "")
    if not bucket_id:
        logger.error("garage_bucket_cors_get missing required param: bucket_id")
        return None
    if not _admin_configured("garage_bucket_cors_get", admin_url, admin_token):
        return None

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_bucket_cors_get(
            progress,
            admin_url=admin_url,
            admin_token=admin_token,
            bucket_id=bucket_id,
        )

    return handler


def make_bucket_cors_set_handler(
    params: dict[str, str],
    *,
    admin_url: str,
    admin_token: str,
) -> JobHandler | None:
    """Build the write handler. Required: ``bucket_id``, ``rules`` and
    ``expected_rules`` (JSON lists, shape-checked at dispatch)."""
    bucket_id = params.get(_BUCKET_ID, "")
    missing = [k for k in (_BUCKET_ID, "rules", "expected_rules") if not params.get(k)]
    if missing:
        logger.error("garage_bucket_cors_set missing required params: %s", missing)
    if missing or not _admin_configured(
        "garage_bucket_cors_set", admin_url, admin_token
    ):
        return None
    try:
        rules = json.loads(params["rules"])
        expected = json.loads(params["expected_rules"])
    except ValueError:
        logger.error("garage_bucket_cors_set: rules or expected_rules is not JSON")
        return None
    for label, value in (("rules", rules), ("expected_rules", expected)):
        problem = cors_rules_shape_problem(value)
        if problem is not None:
            logger.error("garage_bucket_cors_set: %s %s", label, problem)
            return None

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_bucket_cors_set(
            progress,
            admin_url=admin_url,
            admin_token=admin_token,
            bucket_id=bucket_id,
            rules=normalize_cors_rules(rules),
            expected_rules=normalize_cors_rules(expected),
        )

    return handler


async def run_bucket_cors_get(
    progress: ProgressCallback,
    *,
    admin_url: str,
    admin_token: str,
    bucket_id: str,
) -> JobOutcome:
    """GetBucketInfo, return ``{bucket_id, rules}`` in normalized form."""
    started_at = time.monotonic()
    await progress("starting", 0, 1, "Reading bucket CORS rules")
    current, failure = await _read_current_rules(
        admin_url, admin_token, bucket_id, started_at
    )
    if failure is not None:
        return failure
    await progress("finalizing", 1, 1, "CORS rules read")
    return _success(
        bucket_id,
        current,
        started_at,
        f"Bucket {bucket_id} has {len(current)} CORS rule(s)",
    )


def _success(
    bucket_id: str, rules: CorsRules, started_at: float, stdout: str
) -> JobOutcome:
    """The outcome both commands end on: the rules as they now stand."""
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=stdout,
        extras={
            "bucket_id": bucket_id,
            "rules": rules,
            "duration_seconds": _elapsed(started_at),
        },
    )


async def run_bucket_cors_set(
    progress: ProgressCallback,
    *,
    admin_url: str,
    admin_token: str,
    bucket_id: str,
    rules: CorsRules,
    expected_rules: CorsRules,
) -> JobOutcome:
    """Read current rules; refuse as ``cors_rules_stale`` when they differ from
    ``expected_rules``, else POST UpdateBucket. An empty ``rules`` clears."""
    started_at = time.monotonic()
    await progress("starting", 0, 2, "Reading current CORS rules")
    current, failure = await _read_current_rules(
        admin_url, admin_token, bucket_id, started_at
    )
    if failure is not None:
        return failure
    if current != expected_rules:
        # Never write over rules the caller has not seen: return them instead.
        return _failure(
            "cors_rules_stale",
            bucket_id,
            "Bucket CORS rules changed since they were loaded; reload and retry.",
            started_at,
            rules=current,
        )

    await progress("running", 1, 2, "Writing CORS rules")
    ok, err = await asyncio.to_thread(
        admin_api.set_bucket_cors,
        admin_url=admin_url,
        admin_token=admin_token,
        bucket_id=bucket_id,
        rules=rules,
    )
    if not ok:
        return _failure(
            "cors_update_failed",
            bucket_id,
            f"UpdateBucket corsRules failed: {err}",
            started_at,
        )
    await progress("finalizing", 2, 2, "CORS rules applied")
    verb = "Cleared CORS rules on" if not rules else f"Set {len(rules)} CORS rule(s) on"
    return _success(bucket_id, rules, started_at, f"{verb} {bucket_id}")


async def _read_current_rules(
    admin_url: str,
    admin_token: str,
    bucket_id: str,
    started_at: float,
) -> tuple[CorsRules, JobOutcome | None]:
    info, err = await asyncio.to_thread(
        admin_api.get_bucket_info,
        admin_url=admin_url,
        admin_token=admin_token,
        bucket_ref=bucket_id,
    )
    if info is None:
        reason = (
            "bucket_not_found" if admin_api.is_not_found(err) else "bucket_info_failed"
        )
        return [], _failure(reason, bucket_id, err, started_at)
    raw = info.get("corsRules")
    if raw is None:
        return [], None
    # A shape this module does not know is a refusal, never an empty list: an
    # empty read would match expected_rules=[] and license an overwrite.
    problem = cors_rules_shape_problem(raw)
    if problem is not None:
        return [], _failure(
            "cors_rules_unreadable",
            bucket_id,
            f"GetBucketInfo corsRules {problem}",
            started_at,
        )
    return normalize_cors_rules(raw), None


def _admin_configured(command: str, admin_url: str, admin_token: str) -> bool:
    if admin_url and admin_token:
        return True
    logger.error(
        "%s: Garage admin API not configured (admin_url + admin_token); set "
        "[garage] admin_url and admin_token_file.",
        command,
    )
    return False


def _failure(
    failure_reason: str,
    bucket_id: str,
    stderr: str,
    started_at: float,
    *,
    rules: CorsRules | None = None,
) -> JobOutcome:
    extras: dict[str, Any] = {
        "bucket_id": bucket_id,
        "error": stderr,
        "duration_seconds": _elapsed(started_at),
    }
    if rules is not None:
        extras["rules"] = rules
    return JobOutcome(
        success=False,
        exit_code=-1,
        stderr=stderr,
        failure_reason=failure_reason,
        extras=extras,
    )


def _elapsed(started_at: float) -> float:
    return round(time.monotonic() - started_at, 3)
