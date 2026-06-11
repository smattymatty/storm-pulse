"""Garage admin HTTP API client (default port 3903, ``/v2/`` operations).

The agent's other Garage operations go through the CLI over RPC; this is the
one typed HTTP path, used for the BUCKETS-006 quota write (``UpdateBucket``).

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
from typing import Any
from urllib.parse import urlencode, urlparse

_TIMEOUT_SECONDS = 15.0
_FULL_BUCKET_ID_LEN = 64


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


def create_key(
    *, admin_url: str, admin_token: str, name: str,
) -> tuple[dict[str, Any] | None, str]:
    """Create an access key via ``POST /v2/CreateKey``.

    Returns the ``GetKeyInfoResponse`` dict, carrying ``accessKeyId`` and the
    one-time ``secretAccessKey`` (returned only at creation), or ``(None,
    error)``. The secret is never logged here; the caller hands it to the
    operator via the JobOutcome.
    """
    body = json.dumps({"name": name}).encode("utf-8")
    data, err = _post_json(admin_url, admin_token, "/v2/CreateKey", body)
    if data is None:
        return None, err
    if not isinstance(data, dict):
        return None, "CreateKey returned a non-object body"
    return data, ""


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


def delete_key(
    *, admin_url: str, admin_token: str, access_key_id: str,
) -> tuple[bool, str]:
    """Delete an access key via ``POST /v2/DeleteKey?id=<access_key_id>``.

    Returns ``(success, error)``. Used by the provisioning rollback paths, so a
    transport or non-2xx failure surfaces as ``(False, error)``, never raises.
    """
    path = "/v2/DeleteKey?" + urlencode({"id": access_key_id})
    return _post(admin_url, admin_token, path)


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

    try:
        conn = conn_class(parsed.hostname, port, timeout=_TIMEOUT_SECONDS)
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        resp_body = resp.read().decode("utf-8", errors="replace")
        conn.close()
    except (OSError, http.client.HTTPException) as exc:
        return None, f"Could not reach Garage admin API at {admin_url}: {exc}"

    return status, resp_body
