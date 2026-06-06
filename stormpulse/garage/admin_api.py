"""Garage admin HTTP API client (default port 3903, ``/v2/`` operations).

The agent's other Garage operations go through the CLI over RPC; this is the
one typed HTTP path, used for the BUCKETS-006 quota write (``UpdateBucket``).

The admin token is a node secret. Per ADR buckets/000 it lives on the cluster,
in the agent's host environment by virtue of the agent running there, never in
Storm's website DB and never on the WebSocket. The caller resolves the token
(from a file or config) and passes it in; this module only uses it as a Bearer
header against loopback.
"""
from __future__ import annotations

import http.client
import json
from urllib.parse import urlencode, urlparse

_TIMEOUT_SECONDS = 15.0


def set_bucket_quota(
    *, admin_url: str, admin_token: str, bucket_id: str, max_size_bytes: int,
) -> tuple[bool, str]:
    """Set a bucket's max-size quota via ``POST /v2/UpdateBucket``.

    ``max_objects`` is left unlimited (``null``). ``max_size_bytes`` is decimal
    bytes, passed through unchanged so Garage stores exactly what Storm intends.
    Returns ``(success, error_message)``; the message rides to the operator via
    the JobOutcome, it is never customer-facing.
    """
    body = json.dumps(
        {"quotas": {"maxSize": int(max_size_bytes), "maxObjects": None}}
    ).encode("utf-8")
    path = "/v2/UpdateBucket?" + urlencode({"id": bucket_id})
    headers = {
        "Authorization": f"Bearer {admin_token}",
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }
    return _post(admin_url, path, body, headers)


def _post(
    admin_url: str, path: str, body: bytes, headers: dict[str, str],
) -> tuple[bool, str]:
    parsed = urlparse(admin_url)
    if parsed.scheme not in ("http", "https"):
        return False, f"Invalid admin URL scheme: {parsed.scheme!r}"
    if not parsed.hostname:
        return False, f"Admin URL missing hostname: {admin_url!r}"

    conn_class = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        conn = conn_class(parsed.hostname, port, timeout=_TIMEOUT_SECONDS)
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        resp_body = resp.read().decode("utf-8", errors="replace")
        conn.close()
    except (OSError, http.client.HTTPException) as exc:
        return False, f"Could not reach Garage admin API at {admin_url}: {exc}"

    if 200 <= status < 300:
        return True, ""
    return False, f"HTTP {status}: {resp_body.strip()[:500]}"
