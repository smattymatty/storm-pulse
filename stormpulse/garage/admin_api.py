"""Garage admin HTTP client for /v2/ operations (default port 3903).

Callers supply the node-local admin token; keep it off the WebSocket and out
of the website database. Requests authenticate with a Bearer header.
Resolve Storm's 16-character bucket prefixes before writes: the API requires
full 64-character IDs, unlike the CLI."""

from __future__ import annotations

import http.client
import json
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from stormpulse import events

_TIMEOUT_SECONDS = 15.0
_FULL_BUCKET_ID_LEN = 64


# Meter all admin calls, including failures, for endpoint rates and p95 latency.
# The process-wide rolling window survives WebSocket reconnects and exposes
# admin request saturation even when CPU, RAM, and disk look healthy.
_ADMIN_METER_WINDOW_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class AdminCallStats:
    """One endpoint's admin-API call stats over the trailing meter window."""

    sample_count: int
    calls_per_sec: float
    p95_latency_ms: float


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Return the nearest-rank percentile for sorted values; zero if empty.

    For q in [0, 1], use ceil(q * n), clamped to the available ranks.
    Avoid interpolation's false precision with small samples."""
    if not sorted_vals:
        return 0.0
    rank = max(1, math.ceil(q * len(sorted_vals)))
    return sorted_vals[min(rank, len(sorted_vals)) - 1]


class _AdminCallMeter:
    """Track rolling latency samples by admin endpoint for the process lifetime.

    Evict aged samples on record and snapshot; reads never reset the window."""

    def __init__(self, window_seconds: float = _ADMIN_METER_WINDOW_SECONDS) -> None:
        self._window = window_seconds
        self._samples: dict[str, deque[tuple[float, float]]] = {}

    def record(self, admin_url: str, duration_ms: float, now: float) -> None:
        dq = self._samples.get(admin_url)
        if dq is None:
            dq = deque()
            self._samples[admin_url] = dq
        dq.append((now, duration_ms))
        self._evict(dq, now)

    def _evict(self, dq: deque[tuple[float, float]], now: float) -> None:
        cutoff = now - self._window
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def snapshot(self, now: float) -> dict[str, AdminCallStats]:
        """Return per-endpoint rates and p95 latency after evicting old samples.

        Divide counts by the full window duration, so rates ramp up after startup."""
        out: dict[str, AdminCallStats] = {}
        for url, dq in self._samples.items():
            self._evict(dq, now)
            if not dq:
                continue
            durations = sorted(d for _, d in dq)
            out[url] = AdminCallStats(
                sample_count=len(durations),
                calls_per_sec=len(durations) / self._window,
                p95_latency_ms=_percentile(durations, 0.95),
            )
        return out


_METER = _AdminCallMeter()


def admin_call_stats() -> dict[str, AdminCallStats]:
    """Snapshot endpoint statistics for GarageState.admin_metrics."""
    return _METER.snapshot(time.monotonic())


def set_bucket_quota(
    *,
    admin_url: str,
    admin_token: str,
    bucket_id: str,
    max_size_bytes: int,
) -> tuple[bool, str]:
    """Set a bucket's byte quota via POST /v2/UpdateBucket.

    Resolve the bucket prefix first; leave max_objects unlimited.
    Return (success, error_message) for the operator's JobOutcome."""
    auth = {"Authorization": f"Bearer {admin_token}"}
    full_id, err = _resolve_full_bucket_id(admin_url, auth, bucket_id)
    if not full_id:
        return False, err

    body = json.dumps(
        {"quotas": {"maxSize": int(max_size_bytes), "maxObjects": None}}
    ).encode("utf-8")
    headers = {
        **auth,
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }
    path = "/v2/UpdateBucket?" + urlencode({"id": full_id})
    status, resp = _request(admin_url, "POST", path, headers, body)
    if status is None:
        return False, resp
    if 200 <= status < 300:
        return True, ""
    return False, f"HTTP {status}: {resp.strip()[:500]}"


def list_buckets(
    *,
    admin_url: str,
    admin_token: str,
) -> tuple[list[dict[str, Any]] | None, str]:
    """GET /v2/ListBuckets; return (items, "") or (None, error).

    Items include id and globalAliases; use get_bucket_info for details."""
    data, err = _get_json(admin_url, admin_token, "/v2/ListBuckets")
    if data is None:
        return None, err
    if not isinstance(data, list):
        return None, "ListBuckets returned a non-list body"
    return [b for b in data if isinstance(b, dict)], ""


def get_bucket_info(
    *,
    admin_url: str,
    admin_token: str,
    bucket_ref: str,
) -> tuple[dict[str, Any] | None, str]:
    """GET /v2/GetBucketInfo; return (info, "") or (None, error).

    Use id for full IDs and search for prefixes; verify the returned ID matches.
    Info includes integer bytes/objects and quotas.maxSize/maxObjects."""
    if len(bucket_ref) == _FULL_BUCKET_ID_LEN:
        path = "/v2/GetBucketInfo?" + urlencode({"id": bucket_ref})
    else:
        path = "/v2/GetBucketInfo?" + urlencode({"search": bucket_ref})
    data, err = _get_json(admin_url, admin_token, path)
    if data is None:
        return None, err
    if not isinstance(data, dict):
        return None, f"GetBucketInfo {bucket_ref!r} returned a non-object body"
    full = data.get("id", "")
    if not (isinstance(full, str) and full.startswith(bucket_ref)):
        return None, (
            f"GetBucketInfo {bucket_ref!r}: returned id {full!r} does not match "
            "the requested prefix"
        )
    return data, ""


def get_cluster_status(
    *,
    admin_url: str,
    admin_token: str,
) -> tuple[dict[str, Any] | None, str]:
    """GET /v2/GetClusterStatus; return (response, "") or (None, error).

    The nodes array includes identity, version, health, role, and partition data."""
    data, err = _get_json(admin_url, admin_token, "/v2/GetClusterStatus")
    if data is None:
        return None, err
    if not isinstance(data, dict):
        return None, "GetClusterStatus returned a non-object body"
    return data, ""


def get_cluster_statistics(
    *,
    admin_url: str,
    admin_token: str,
) -> tuple[dict[str, Any] | None, str]:
    """GET /v2/GetClusterStatistics; return (response, "") or (None, error).

    Includes structured counts, byte totals, dataAvail, and freeform text.
    The agent reads totalObjectCount."""
    data, err = _get_json(admin_url, admin_token, "/v2/GetClusterStatistics")
    if data is None:
        return None, err
    if not isinstance(data, dict):
        return None, "GetClusterStatistics returned a non-object body"
    return data, ""


def list_keys(
    *,
    admin_url: str,
    admin_token: str,
) -> tuple[list[dict[str, Any]] | None, str]:
    """GET /v2/ListKeys; return (items, "") or (None, error).

    Items contain id and name, never secrets."""
    data, err = _get_json(admin_url, admin_token, "/v2/ListKeys")
    if data is None:
        return None, err
    if not isinstance(data, list):
        return None, "ListKeys returned a non-list body"
    return [k for k in data if isinstance(k, dict)], ""


def get_key_info(
    *,
    admin_url: str,
    admin_token: str,
    access_key_id: str,
) -> tuple[dict[str, Any] | None, str]:
    """GET /v2/GetKeyInfo by access key ID; return (info, "") or (None, error).

    Includes bucket permissions; omit showSecretKey to avoid returning secrets."""
    path = "/v2/GetKeyInfo?" + urlencode({"id": access_key_id})
    data, err = _get_json(admin_url, admin_token, path)
    if data is None:
        return None, err
    if not isinstance(data, dict):
        return None, "GetKeyInfo returned a non-object body"
    return data, ""


def create_key(
    *,
    admin_url: str,
    admin_token: str,
    name: str,
    allow_create_bucket: bool = False,
) -> tuple[dict[str, Any] | None, str]:
    """POST /v2/CreateKey; return (info, "") or (None, error).

    Info includes accessKeyId and secretAccessKey; never log the secret.
    The caller delivers it to the operator through JobOutcome.
    S3 CreateBucket permission defaults to disabled."""
    payload: dict[str, Any] = {"name": name}
    if allow_create_bucket:
        payload["allow"] = {"createBucket": True}
    body = json.dumps(payload).encode("utf-8")
    data, err = _post_json(admin_url, admin_token, "/v2/CreateKey", body)
    if data is None:
        return None, err
    if not isinstance(data, dict):
        return None, "CreateKey returned a non-object body"
    return data, ""


def update_key(
    *,
    admin_url: str,
    admin_token: str,
    access_key_id: str,
    allow_create_bucket: bool,
) -> tuple[bool, str]:
    """Toggle S3 CreateBucket via POST /v2/UpdateKey; return (success, error).

    Send allow.createBucket or deny.createBucket to enforce the bucket-count limit."""
    block = "allow" if allow_create_bucket else "deny"
    body = json.dumps({block: {"createBucket": True}}).encode("utf-8")
    path = "/v2/UpdateKey?" + urlencode({"id": access_key_id})
    return _post(admin_url, admin_token, path, body)


def create_bucket(
    *,
    admin_url: str,
    admin_token: str,
    local_alias: dict[str, Any] | None = None,
    global_alias: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """POST /v2/CreateBucket; return (info, "") or (None, error).

    Aliases are optional. local_alias supplies accessKeyId, alias, and
    allow permissions (read/write/owner) for atomic creation and key binding.
    The response includes the full bucket ID."""
    payload: dict[str, Any] = {}
    if global_alias is not None:
        payload["globalAlias"] = global_alias
    if local_alias is not None:
        payload["localAlias"] = local_alias
    body = json.dumps(payload).encode("utf-8")
    data, err = _post_json(admin_url, admin_token, "/v2/CreateBucket", body)
    if data is None:
        return None, err
    if not isinstance(data, dict):
        return None, "CreateBucket returned a non-object body"
    return data, ""


def delete_bucket(
    *,
    admin_url: str,
    admin_token: str,
    bucket_ref: str,
) -> tuple[bool, str]:
    """POST /v2/DeleteBucket after resolving the full ID; return (success, error).

    Garage requires an empty bucket and removes all global and local aliases."""
    auth = {"Authorization": f"Bearer {admin_token}"}
    full_id, err = _resolve_full_bucket_id(admin_url, auth, bucket_ref)
    if not full_id:
        return False, err
    path = "/v2/DeleteBucket?" + urlencode({"id": full_id})
    return _post(admin_url, admin_token, path)


def delete_key(
    *,
    admin_url: str,
    admin_token: str,
    access_key_id: str,
) -> tuple[bool, str]:
    """POST /v2/DeleteKey; return (success, error).

    Provisioning rollback receives transport and HTTP failures as (False, error)."""
    path = "/v2/DeleteKey?" + urlencode({"id": access_key_id})
    return _post(admin_url, admin_token, path)


def cleanup_incomplete_uploads(
    *,
    admin_url: str,
    admin_token: str,
    bucket_ref: str,
    older_than_secs: int,
) -> tuple[int | None, str]:
    """Abort multipart uploads older than older_than_secs.

    POST /v2/CleanupIncompleteUploads requires bucketId and olderThanSecs.
    Return (uploadsDeleted, "") or (None, error). The age cutoff protects
    recent uploads; there is deliberately no abort-all shortcut."""
    auth = {"Authorization": f"Bearer {admin_token}"}
    full_id, err = _resolve_full_bucket_id(admin_url, auth, bucket_ref)
    if not full_id:
        return None, err
    body = json.dumps(
        {"bucketId": full_id, "olderThanSecs": int(older_than_secs)}
    ).encode("utf-8")
    data, err = _post_json(admin_url, admin_token, "/v2/CleanupIncompleteUploads", body)
    if data is None:
        return None, err
    if not isinstance(data, dict) or "uploadsDeleted" not in data:
        return None, "CleanupIncompleteUploads returned an unexpected body"
    return int(data["uploadsDeleted"]), ""


def is_not_found(err: str) -> bool:
    """Recognize 404, not found, NoSuchBucket, or NoSuchKey error strings.

    Used by idempotent deletes and tombstone cleanup to accept absent resources."""
    low = err.lower()
    return any(s in low for s in ("404", "not found", "nosuchbucket", "nosuchkey"))


def allow_bucket_key(
    *,
    admin_url: str,
    admin_token: str,
    bucket_ref: str,
    access_key_id: str,
    read: bool,
    write: bool,
    owner: bool = False,
) -> tuple[bool, str]:
    """POST /v2/AllowBucketKey after resolving the full bucket ID.

    Return (success, error)."""
    return _bucket_key_perm_change(
        "/v2/AllowBucketKey",
        admin_url,
        admin_token,
        bucket_ref,
        access_key_id,
        read=read,
        write=write,
        owner=owner,
    )


def deny_bucket_key(
    *,
    admin_url: str,
    admin_token: str,
    bucket_ref: str,
    access_key_id: str,
    read: bool,
    write: bool,
    owner: bool = False,
) -> tuple[bool, str]:
    """POST /v2/DenyBucketKey to revoke permissions; return (success, error)."""
    return _bucket_key_perm_change(
        "/v2/DenyBucketKey",
        admin_url,
        admin_token,
        bucket_ref,
        access_key_id,
        read=read,
        write=write,
        owner=owner,
    )


def add_bucket_alias_local(
    *,
    admin_url: str,
    admin_token: str,
    bucket_ref: str,
    access_key_id: str,
    local_alias: str,
) -> tuple[bool, str]:
    """POST /v2/AddBucketAlias to bind an alias in a key's namespace.

    Resolve the full bucket ID first; return (success, error)."""
    return _bucket_alias_local_change(
        "/v2/AddBucketAlias",
        admin_url,
        admin_token,
        bucket_ref,
        access_key_id,
        local_alias,
    )


def remove_bucket_alias_local(
    *,
    admin_url: str,
    admin_token: str,
    bucket_ref: str,
    access_key_id: str,
    local_alias: str,
) -> tuple[bool, str]:
    """POST /v2/RemoveBucketAlias to unbind a key's local alias.

    Used by provisioning rollback; return (success, error)."""
    return _bucket_alias_local_change(
        "/v2/RemoveBucketAlias",
        admin_url,
        admin_token,
        bucket_ref,
        access_key_id,
        local_alias,
    )


def _bucket_alias_local_change(
    path: str,
    admin_url: str,
    admin_token: str,
    bucket_ref: str,
    access_key_id: str,
    local_alias: str,
) -> tuple[bool, str]:
    """Resolve ``bucket_ref`` to the full id and POST an Add/Remove local alias."""
    auth = {"Authorization": f"Bearer {admin_token}"}
    full_id, err = _resolve_full_bucket_id(admin_url, auth, bucket_ref)
    if not full_id:
        return False, err
    body = json.dumps(
        {"bucketId": full_id, "localAlias": local_alias, "accessKeyId": access_key_id}
    ).encode("utf-8")
    return _post(admin_url, admin_token, path, body)


def _bucket_key_perm_change(
    path: str,
    admin_url: str,
    admin_token: str,
    bucket_ref: str,
    access_key_id: str,
    *,
    read: bool,
    write: bool,
    owner: bool,
) -> tuple[bool, str]:
    """Resolve ``bucket_ref`` to the full id and POST an Allow/Deny perm change."""
    auth = {"Authorization": f"Bearer {admin_token}"}
    full_id, err = _resolve_full_bucket_id(admin_url, auth, bucket_ref)
    if not full_id:
        return False, err
    body = json.dumps(
        {
            "bucketId": full_id,
            "accessKeyId": access_key_id,
            "permissions": {"read": read, "write": write, "owner": owner},
        }
    ).encode("utf-8")
    return _post(admin_url, admin_token, path, body)


def _get_json(
    admin_url: str,
    admin_token: str,
    path: str,
) -> tuple[object | None, str]:
    """GET ``path`` and parse a JSON body. Returns ``(parsed, "")`` or
    ``(None, error)`` on transport, status, or decode failure."""
    auth = {"Authorization": f"Bearer {admin_token}"}
    status, resp = _request(admin_url, "GET", path, auth)
    if status is None:
        return None, resp
    if not (200 <= status < 300):
        return None, f"HTTP {status}: {resp.strip()[:300]}"
    try:
        return json.loads(resp), ""
    except json.JSONDecodeError:
        return None, f"admin API returned non-JSON for {path}"


def _post(
    admin_url: str,
    admin_token: str,
    path: str,
    body: bytes | None = None,
) -> tuple[bool, str]:
    """POST ``path`` (optionally with a JSON ``body``) and check the status.

    Returns ``(True, "")`` on 2xx, else ``(False, error)``. The response body is
    discarded; callers that need it use :func:`_post_json`.
    """
    headers = {"Authorization": f"Bearer {admin_token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(body))
    status, resp = _request(admin_url, "POST", path, headers, body)
    if status is None:
        return False, resp
    if 200 <= status < 300:
        return True, ""
    return False, f"HTTP {status}: {resp.strip()[:500]}"


def _post_json(
    admin_url: str,
    admin_token: str,
    path: str,
    body: bytes,
) -> tuple[object | None, str]:
    """POST ``body`` to ``path`` and parse a JSON response. Returns
    ``(parsed, "")`` or ``(None, error)`` on transport, status, or decode
    failure."""
    headers = {
        "Authorization": f"Bearer {admin_token}",
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }
    status, resp = _request(admin_url, "POST", path, headers, body)
    if status is None:
        return None, resp
    if not (200 <= status < 300):
        return None, f"HTTP {status}: {resp.strip()[:300]}"
    try:
        return json.loads(resp), ""
    except json.JSONDecodeError:
        return None, f"admin API returned non-JSON for {path}"


def _resolve_full_bucket_id(
    admin_url: str,
    auth: dict[str, str],
    bucket_id: str,
) -> tuple[str, str]:
    """Resolve a bucket prefix to a full ID; pass full IDs through unchanged.

    Verify search results start with the prefix to reject alias collisions.
    Return (full_id, "") or ("", error)."""
    if len(bucket_id) == _FULL_BUCKET_ID_LEN:
        return bucket_id, ""
    path = "/v2/GetBucketInfo?" + urlencode({"search": bucket_id})
    status, resp = _request(admin_url, "GET", path, auth)
    if status is None:
        return "", resp
    if not (200 <= status < 300):
        return (
            "",
            f"resolve bucket id {bucket_id!r}: HTTP {status}: {resp.strip()[:300]}",
        )
    try:
        info = json.loads(resp)
    except json.JSONDecodeError:
        return "", f"resolve bucket id {bucket_id!r}: admin API returned non-JSON"
    full = info.get("id", "") if isinstance(info, dict) else ""
    if not (isinstance(full, str) and full.startswith(bucket_id)):
        return "", f"resolve bucket id {bucket_id!r}: no bucket matched the prefix"
    return full, ""


def _event_target(endpoint: str, path: str) -> dict[str, str]:
    """Map a request's id query parameter to bucket_id or key_id event fields.

    Return no fields for endpoints without a recognized resource ID."""
    target_id = parse_qs(urlparse(path).query).get("id", [""])[0]
    if not target_id:
        return {}
    if "Bucket" in endpoint:
        return {"bucket_id": target_id}
    if "Key" in endpoint:
        return {"key_id": target_id}
    return {}


def _request(
    admin_url: str,
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes | None = None,
) -> tuple[int | None, str]:
    """Issue one admin-API request. Returns ``(status, body)`` or ``(None, err)``
    when the endpoint can't be reached."""
    parsed = urlparse(admin_url)
    if parsed.scheme not in ("http", "https"):
        return None, f"Invalid admin URL scheme: {parsed.scheme!r}"
    if not parsed.hostname:
        return None, f"Admin URL missing hostname: {admin_url!r}"

    conn_class = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    # Time the full request/response, including failures, after URL validation.
    start = time.monotonic()
    try:
        conn = conn_class(parsed.hostname, port, timeout=_TIMEOUT_SECONDS)
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        resp_body = resp.read().decode("utf-8", errors="replace")
        conn.close()
        result: tuple[int | None, str] = (status, resp_body)
    except (OSError, http.client.HTTPException) as exc:
        result = (None, f"Could not reach Garage admin API at {admin_url}: {exc}")
    finally:
        now = time.monotonic()
        duration_ms = (now - start) * 1000.0
        _METER.record(admin_url, duration_ms, now)
        # Emit raw call details so the control plane can compute other aggregates.
        endpoint = path.split("?", 1)[0].rsplit("/", 1)[-1]
        events.emit(
            "admin_call",
            source="garage_admin",
            endpoint=endpoint,
            http_method=method,
            duration_ms=int(duration_ms),
            status=result[0],
            error=result[1] if result[0] is None else "",
            **_event_target(endpoint, path),
        )

    return result
