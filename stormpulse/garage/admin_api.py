"""Garage admin HTTP API client (default port 3903, ``/v2/`` operations).

The agent's other Garage operations go through the CLI over RPC; this is the
one typed HTTP path, used for the quota write (``UpdateBucket``).

The admin token is a node secret. Per ADR buckets/000 it lives on the cluster,
in the agent's host environment by virtue of the agent running there, never in
Storm's website DB and never on the WebSocket. The caller resolves the token
and passes it in; this module only uses it as a Bearer header against loopback.

The admin API addresses buckets by their **full 64-char id** and rejects the
16-char prefix Storm stores in ``garage_bucket_id`` (unlike the CLI, which does
prefix matching). So the prefix is resolved to the full id first.
"""
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


# --- Admin-API call meter (observability) ----------------------------------
# Every admin call routes through ``_request``, so wrapping it once meters 100%
# of admin traffic - state reads, the detector, and command-driven mutations
# alike. The meter holds a trailing time window of per-call latencies keyed by
# target endpoint, so the agent reports admin-API call-rate and p95 latency per
# target node on the metrics push.
#
# This is the signal the 2026-06-27 saturation incident had no graph for: the
# saturating resource was admin-API request serialization while CPU/RAM/disk all
# read healthy. The meter is a process singleton (admin load is a property of the
# process talking to the node, and rightly survives a websocket reconnect like
# the state reader's topology cache). It records, never blocks, and meters
# failures too: a timed-out call still consumed admin time and is the most
# important latency to see under saturation.
_ADMIN_METER_WINDOW_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class AdminCallStats:
    """One endpoint's admin-API call stats over the trailing meter window."""

    sample_count: int
    calls_per_sec: float
    p95_latency_ms: float


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank percentile of a pre-sorted list (``q`` in [0, 1]); 0.0 if empty.

    Nearest-rank, not interpolated: with the handful of admin calls per window a
    small agent makes, an interpolated p95 buys false precision. The rank is
    ``ceil(q * n)`` clamped into range, so p95 of 20 samples is the 19th.
    """
    if not sorted_vals:
        return 0.0
    rank = max(1, math.ceil(q * len(sorted_vals)))
    return sorted_vals[min(rank, len(sorted_vals)) - 1]


class _AdminCallMeter:
    """Trailing-window admin-API latency samples, keyed by endpoint (``admin_url``).

    Per endpoint a deque of ``(monotonic_ts, duration_ms)``; samples older than
    the window are evicted on every record and snapshot, so memory is bounded by
    the call rate, not by uptime. Stateless across nothing: one process-lifetime
    instance, never reset on read (the window is rolling, evicted by age).
    """

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
        """Read the current window per endpoint. Eviction-only; never clears.

        ``calls_per_sec`` is the window's sample count over the FULL window span,
        so it averages over the trailing window (a cold-started agent ramps to
        the true rate over one window, never spikes). ``p95`` is over the same
        surviving samples.
        """
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
    """Snapshot the admin-API meter, keyed by endpoint.

    The garage state read folds this into the per-node ``admin_metrics`` it puts
    on the metrics push (``state.GarageState.admin_metrics``).
    """
    return _METER.snapshot(time.monotonic())


def set_bucket_quota(
    *, admin_url: str, admin_token: str, bucket_id: str, max_size_bytes: int,
) -> tuple[bool, str]:
    """Set a bucket's max-size quota via ``POST /v2/UpdateBucket``.

    Resolves Storm's 16-char ``bucket_id`` to Garage's full id first, then sets
    the quota (``max_objects`` left unlimited). ``max_size_bytes`` is decimal
    bytes, passed through unchanged. Returns ``(success, error_message)``; the
    message rides to the operator via the JobOutcome, never customer-facing.
    """
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
    *, admin_url: str, admin_token: str,
) -> tuple[list[dict[str, Any]] | None, str]:
    """List every bucket via ``GET /v2/ListBuckets``.

    Returns ``(items, "")`` where each item carries at least ``id`` and
    ``globalAliases`` (per the v2 ``ListBucketsResponseItem`` schema), or
    ``(None, error)`` when the endpoint can't be reached or returns non-2xx.
    The caller fetches per-bucket detail via :func:`get_bucket_info`.
    """
    data, err = _get_json(admin_url, admin_token, "/v2/ListBuckets")
    if data is None:
        return None, err
    if not isinstance(data, list):
        return None, "ListBuckets returned a non-list body"
    return [b for b in data if isinstance(b, dict)], ""


def get_bucket_info(
    *, admin_url: str, admin_token: str, bucket_ref: str,
) -> tuple[dict[str, Any] | None, str]:
    """Fetch one bucket's full info via ``GET /v2/GetBucketInfo``.

    ``bucket_ref`` may be Garage's full 64-char id (looked up exactly via
    ``?id=``) or Storm's 16-char prefix (resolved via ``?search=``). When the
    prefix path is used we verify the returned ``id`` actually starts with it,
    so a partial-match collision can never return the wrong bucket's stats.

    Returns the parsed ``GetBucketInfoResponse`` dict (exact integer
    ``bytes``/``objects`` and ``quotas.maxSize``/``maxObjects``, JSON, never
    scraped text), or ``(None, error)``.
    """
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
    *, admin_url: str, admin_token: str,
) -> tuple[dict[str, Any] | None, str]:
    """Cluster node list via ``GET /v2/GetClusterStatus``.

    Returns the response dict (``nodes`` array of ``NodeResp``: ``id``,
    ``hostname``, ``addr``, ``garageVersion``, ``isUp``, ``role.zone`` /
    ``role.capacity``, ``dataPartition``), or ``(None, error)``.
    """
    data, err = _get_json(admin_url, admin_token, "/v2/GetClusterStatus")
    if data is None:
        return None, err
    if not isinstance(data, dict):
        return None, "GetClusterStatus returned a non-object body"
    return data, ""


def get_cluster_statistics(
    *, admin_url: str, admin_token: str,
) -> tuple[dict[str, Any] | None, str]:
    """Cluster statistics via ``GET /v2/GetClusterStatistics``.

    Returns the response dict (structured ``bucketCount`` / ``totalObjectCount`` /
    ``totalObjectBytes`` / ``dataAvail`` plus a ``freeform`` text blob), or
    ``(None, error)``. The agent reads the structured ``totalObjectCount``.
    """
    data, err = _get_json(admin_url, admin_token, "/v2/GetClusterStatistics")
    if data is None:
        return None, err
    if not isinstance(data, dict):
        return None, "GetClusterStatistics returned a non-object body"
    return data, ""


def list_keys(
    *, admin_url: str, admin_token: str,
) -> tuple[list[dict[str, Any]] | None, str]:
    """List access keys via ``GET /v2/ListKeys``.

    Returns ``(items, "")`` where each item carries ``id`` and ``name``, or
    ``(None, error)``. Secrets are never in this response.
    """
    data, err = _get_json(admin_url, admin_token, "/v2/ListKeys")
    if data is None:
        return None, err
    if not isinstance(data, list):
        return None, "ListKeys returned a non-list body"
    return [k for k in data if isinstance(k, dict)], ""


def get_key_info(
    *, admin_url: str, admin_token: str, access_key_id: str,
) -> tuple[dict[str, Any] | None, str]:
    """Fetch one access key's info via ``GET /v2/GetKeyInfo?id=<access_key_id>``.

    Returns the ``GetKeyInfoResponse`` dict (carrying a ``buckets`` array of
    every bucket this key has permissions on), or ``(None, error)``. The secret
    is not requested (``showSecretKey`` omitted), so it is never in the response.
    """
    path = "/v2/GetKeyInfo?" + urlencode({"id": access_key_id})
    data, err = _get_json(admin_url, admin_token, path)
    if data is None:
        return None, err
    if not isinstance(data, dict):
        return None, "GetKeyInfo returned a non-object body"
    return data, ""


def create_key(
    *, admin_url: str, admin_token: str, name: str,
    allow_create_bucket: bool = False,
) -> tuple[dict[str, Any] | None, str]:
    """Create an access key via ``POST /v2/CreateKey``.

    Returns the ``GetKeyInfoResponse`` dict, carrying ``accessKeyId`` and the
    one-time ``secretAccessKey`` (returned only at creation), or ``(None,
    error)``. The secret is never logged here; the caller hands it to the
    operator via the JobOutcome. ``allow_create_bucket`` sets the key-level
    S3 CreateBucket capability at mint (the account key); the
    default of off matches Garage.
    """
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
    *, admin_url: str, admin_token: str, access_key_id: str,
    allow_create_bucket: bool,
) -> tuple[bool, str]:
    """Toggle a key's S3 CreateBucket capability via ``POST /v2/UpdateKey``.

    Sends ``allow.createBucket`` when ``allow_create_bucket`` is True, else
    ``deny.createBucket`` (Garage's allow/deny block sets the key-level
    ``allow_create_bucket`` flag). This is the count-backstop
    lever: flipped off past the bucket-count rail, back on when room opens.
    Returns ``(success, error)``.
    """
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
    """Create a bucket via ``POST /v2/CreateBucket``.

    Both aliases are optional; omit both for an alias-less bucket. A
    ``local_alias`` of ``{"accessKeyId", "alias", "allow": {read,write,owner}}``
    makes Garage atomically create the bucket, bind that key's local alias, and
    grant its permissions in one call. Returns the ``GetBucketInfoResponse``
    dict (carrying the full ``id``), or ``(None, error)``.
    """
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
    *, admin_url: str, admin_token: str, bucket_ref: str,
) -> tuple[bool, str]:
    """Delete a bucket via ``POST /v2/DeleteBucket?id=<full_id>``.

    Resolves ``bucket_ref`` to the full id first (the admin API rejects the
    16-char prefix). DeleteBucket removes the bucket together with **all** its
    aliases (global and local) in one call, and Garage rejects it unless the
    bucket is empty. Returns ``(success, error)``.
    """
    auth = {"Authorization": f"Bearer {admin_token}"}
    full_id, err = _resolve_full_bucket_id(admin_url, auth, bucket_ref)
    if not full_id:
        return False, err
    path = "/v2/DeleteBucket?" + urlencode({"id": full_id})
    return _post(admin_url, admin_token, path)


def delete_key(
    *, admin_url: str, admin_token: str, access_key_id: str,
) -> tuple[bool, str]:
    """Delete an access key via ``POST /v2/DeleteKey?id=<access_key_id>``.

    Returns ``(success, error)``. Used by the provisioning rollback paths, so a
    transport or non-2xx failure surfaces as ``(False, error)``, never raises.
    """
    path = "/v2/DeleteKey?" + urlencode({"id": access_key_id})
    return _post(admin_url, admin_token, path)


def cleanup_incomplete_uploads(
    *, admin_url: str, admin_token: str, bucket_ref: str, older_than_secs: int,
) -> tuple[int | None, str]:
    """Abort a bucket's incomplete multipart uploads older than a cutoff.

    ``POST /v2/CleanupIncompleteUploads`` with ``{bucketId, olderThanSecs}``
    (both required by Garage); returns ``(uploadsDeleted, "")`` or
    ``(None, error)``.

    The age cutoff is the whole safety of this call. An in-flight upload from
    seconds ago is a live customer operation; one from days ago is garbage
    holding disk. Aborting by age keeps the fail-safe direction (data it cannot
    classify as garbage is kept), which is why there is no "abort everything"
    convenience here.
    """
    auth = {"Authorization": f"Bearer {admin_token}"}
    full_id, err = _resolve_full_bucket_id(admin_url, auth, bucket_ref)
    if not full_id:
        return None, err
    body = json.dumps(
        {"bucketId": full_id, "olderThanSecs": int(older_than_secs)}
    ).encode("utf-8")
    data, err = _post_json(
        admin_url, admin_token, "/v2/CleanupIncompleteUploads", body
    )
    if data is None:
        return None, err
    if not isinstance(data, dict) or "uploadsDeleted" not in data:
        return None, "CleanupIncompleteUploads returned an unexpected body"
    return int(data["uploadsDeleted"]), ""


def is_not_found(err: str) -> bool:
    """True if an admin-API error string means the resource is already gone.

    A 404 surfaces from :func:`_post` as ``"HTTP 404: ..."``; Garage's own
    bodies use ``NoSuchBucket`` / ``NoSuchKey``. Callers that treat
    already-absent as success (idempotent deletes, the credential-kill
    tombstone sweep) use this to tell "confirmed gone" from a transient
    error.
    """
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
    """Grant a key permissions on a bucket via ``POST /v2/AllowBucketKey``.

    ``bucket_ref`` is Storm's 16-char prefix (or a full id); it is resolved to
    Garage's full 64-char id first, since the admin API rejects the prefix.
    Returns ``(success, error)``.
    """
    return _bucket_key_perm_change(
        "/v2/AllowBucketKey", admin_url, admin_token, bucket_ref,
        access_key_id, read, write, owner,
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
    """Revoke a key's permissions on a bucket via ``POST /v2/DenyBucketKey``.

    Same shape as :func:`allow_bucket_key`; used by provisioning rollback to
    undo a grant. Returns ``(success, error)``.
    """
    return _bucket_key_perm_change(
        "/v2/DenyBucketKey", admin_url, admin_token, bucket_ref,
        access_key_id, read, write, owner,
    )


def add_bucket_alias_local(
    *,
    admin_url: str,
    admin_token: str,
    bucket_ref: str,
    access_key_id: str,
    local_alias: str,
) -> tuple[bool, str]:
    """Attach a local alias to a bucket in a key's namespace, via the local
    variant of ``POST /v2/AddBucketAlias``.

    ``bucket_ref`` is resolved to the full id first. Returns ``(success,
    error)``.
    """
    return _bucket_alias_local_change(
        "/v2/AddBucketAlias", admin_url, admin_token, bucket_ref,
        access_key_id, local_alias,
    )


def remove_bucket_alias_local(
    *,
    admin_url: str,
    admin_token: str,
    bucket_ref: str,
    access_key_id: str,
    local_alias: str,
) -> tuple[bool, str]:
    """Detach a local alias from a bucket in a key's namespace, via the local
    variant of ``POST /v2/RemoveBucketAlias``.

    Same shape as :func:`add_bucket_alias_local`; used by provisioning rollback
    to undo an attached alias. Returns ``(success, error)``.
    """
    return _bucket_alias_local_change(
        "/v2/RemoveBucketAlias", admin_url, admin_token, bucket_ref,
        access_key_id, local_alias,
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
    admin_url: str, admin_token: str, path: str,
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
    admin_url: str, admin_token: str, path: str, body: bytes | None = None,
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
    admin_url: str, admin_token: str, path: str, body: bytes,
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
    admin_url: str, auth: dict[str, str], bucket_id: str,
) -> tuple[str, str]:
    """Resolve Storm's 16-char ``garage_bucket_id`` to Garage's full 64-char id.

    ``GetBucketInfo``'s ``search`` param does a partial match; we verify the
    returned id actually starts with the prefix, so an alias collision can never
    redirect the write to the wrong bucket. A full id passes straight through.
    Returns ``(full_id, "")`` or ``("", error)``.
    """
    if len(bucket_id) == _FULL_BUCKET_ID_LEN:
        return bucket_id, ""
    path = "/v2/GetBucketInfo?" + urlencode({"search": bucket_id})
    status, resp = _request(admin_url, "GET", path, auth)
    if status is None:
        return "", resp
    if not (200 <= status < 300):
        return "", f"resolve bucket id {bucket_id!r}: HTTP {status}: {resp.strip()[:300]}"
    try:
        info = json.loads(resp)
    except json.JSONDecodeError:
        return "", f"resolve bucket id {bucket_id!r}: admin API returned non-JSON"
    full = info.get("id", "") if isinstance(info, dict) else ""
    if not (isinstance(full, str) and full.startswith(bucket_id)):
        return "", f"resolve bucket id {bucket_id!r}: no bucket matched the prefix"
    return full, ""


def _event_target(endpoint: str, path: str) -> dict[str, str]:
    """The admin call's target resource, as event fields.

    Per-resource endpoints carry their target as ``?id=``; attribute it
    to the right typed column by endpoint family, so events answer
    "which bucket (or key) was this call about". Endpoints without an
    id (list/cluster calls) contribute nothing.
    """
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

    # Meter every real call attempt (past URL validation), success or failure:
    # a timeout consumed admin time and is the saturation signal. Timed across
    # the whole request/response so latency reflects what the admin API took.
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
        # One wide event per call: the raw record the meter's aggregate is
        # derived FROM, control-plane side, at read time. The meter freezes
        # the questions it was built to answer; the event answers the ones
        # nobody has asked yet (write-time-aggregation scar, 2026-06-27).
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
