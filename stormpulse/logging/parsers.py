"""Strict per-format parsers for log lines.

Each parser takes one raw line and returns a parsed dict or ``None``.
Regex or structured JSON only - never eval, exec, or interpret content.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, cast

MAX_LINE_BYTES = 4096


def _truncate(line: str, max_bytes: int = MAX_LINE_BYTES) -> tuple[str, bool]:
    """Truncate a line to max_bytes UTF-8 bytes. Returns (line, was_truncated)."""
    encoded = line.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return line, False
    return encoded[:max_bytes].decode("utf-8", errors="ignore"), True


# Garage S3 access log format:
#   2026-04-10T13:23:51.766230Z  INFO garage_api_common::generic_server: 71.19.243.102 (via [::1]:37780) (key GKc8a2eafe464b4754187172d0) HEAD /usr-1-obsidian-vault
_GARAGE_S3_RE = re.compile(
    r"(?P<ts>\S+)\s+INFO\s+garage_api_common::generic_server:\s+"
    r"(?P<ip>\S+)\s+\(via\s+(?P<proxy>[^)]+)\)\s+"
    r"\(key\s+(?P<key_id>[A-Za-z0-9]+)\)\s+"
    r"(?P<method>[A-Z]+)\s+"
    r"(?P<path>\S+)\s*$"
)

_GARAGE_ADMIN_RE = re.compile(
    r"(?P<ts>\S+)\s+INFO\s+garage_api_admin::api_server:\s+"
    r"(?:Proxied|Internal)\s+admin\s+API\s+request:\s+"
    r"(?P<operation>\S+)\s*$"
)

# Only ship mutating admin operations. The agent's own garage_refresh
# polls GetBucketInfo/ListKeys/GetClusterStatus every 30s - that's
# noise, not operator-initiated activity worth tracking.
_GARAGE_ADMIN_MUTATIONS: frozenset[str] = frozenset(
    {
        "CreateBucket",
        "DeleteBucket",
        "UpdateBucket",
        "CreateKey",
        "DeleteKey",
        "UpdateKey",
        "AllowBucketKey",
        "DenyBucketKey",
        "ApplyClusterLayout",
    }
)


def parse_garage_s3(line: str) -> dict[str, Any] | None:
    """Parse a Garage S3 access log line.

    Returns ``None`` for non-matching lines, admin API noise, or
    anything that doesn't look like a customer-facing request.
    """
    stripped = _ANSI_ESCAPE_RE.sub("", line.rstrip("\r\n"))
    # Docker source prepends its own timestamp. Strip it only when the
    # remainder still begins with a timestamp (the original Garage one).
    docker_prefix = _DOCKER_TS_RE.match(stripped)
    if docker_prefix is not None and _DOCKER_TS_RE.match(docker_prefix.group(2)):
        stripped = docker_prefix.group(2)
    truncated_line, truncated = _truncate(stripped)

    # Try the S3 access log format first (customer-facing requests).
    m = _GARAGE_S3_RE.fullmatch(truncated_line)
    if m is not None:
        path = m.group("path")
        bucket = ""
        object_key = ""
        if path.startswith("/"):
            path_part = path.split("?", 1)[0]
            parts = path_part[1:].split("/", 1)
            bucket = parts[0]
            object_key = parts[1] if len(parts) > 1 else ""

        method = m.group("method")
        if object_key:
            message = f"{method} {bucket}/{object_key}"
        elif bucket:
            message = f"{method} {bucket}"
        else:
            message = f"{method} {path}"

        return {
            "ts": m.group("ts"),
            "level": "info",
            "message": message,
            "client_ip": m.group("ip"),
            "proxy": m.group("proxy"),
            "key_id": m.group("key_id"),
            "method": method,
            "path": path,
            "bucket": bucket,
            "object_key": object_key,
            "response_code": None,
            "truncated": truncated,
        }

    # Fall back to mutating admin API operations only.
    # Read-only polls (GetBucketInfo, ListKeys, etc.) are dropped - the
    # agent's own garage_refresh generates these every 30s.
    am = _GARAGE_ADMIN_RE.fullmatch(truncated_line)
    if am is not None:
        operation = am.group("operation")
        if operation not in _GARAGE_ADMIN_MUTATIONS:
            return None
        return {
            "ts": am.group("ts"),
            "level": "info",
            "message": operation,
            "client_ip": "",
            "proxy": "",
            "key_id": "",
            "method": "ADMIN",
            "path": "",
            "bucket": "",
            "object_key": "",
            "response_code": None,
            "truncated": truncated,
        }

    return None


_STORMPULSE_REQUIRED = {"ts", "level", "message", "event_type"}


def parse_stormpulse(line: str) -> dict[str, Any] | None:
    """Parse a Storm Pulse structured JSON log line.

    Expects one JSON object per line with at least ts/level/message/event_type.
    Returns ``None`` for malformed JSON or missing required fields.
    """
    stripped = line.rstrip("\r\n").strip()
    if not stripped:
        return None

    truncated_line, truncated = _truncate(stripped)
    try:
        obj = json.loads(truncated_line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None

    missing = _STORMPULSE_REQUIRED - obj.keys()
    if missing:
        return None

    # Copy only primitive/JSON-safe fields to prevent shipping untyped Python objects
    result: dict[str, Any] = {
        "ts": obj["ts"],
        "level": obj["level"],
        "message": obj["message"],
        "event_type": obj["event_type"],
        "truncated": truncated,
    }
    for optional_key in ("command", "success", "duration_ms", "detail"):
        if optional_key in obj:
            result[optional_key] = obj[optional_key]
    return result


_CADDY_REQUIRED = {"ts", "request", "status"}
_CERT_LOGGER_PREFIX = "tls"
_DOCKER_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+(.*)$")
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_UA_BROWSER_RE = re.compile(
    r"(Firefox|Chrome|Safari|Edge|Opera|curl|wget|Go-http-client|python-requests)/[\d.]+"
)


def _summarize_user_agent(ua: str) -> str:
    """Pick a short tag from a User-Agent header (e.g. 'Firefox/134.0')."""
    if not ua:
        return ""
    m = _UA_BROWSER_RE.search(ua)
    return m.group(0) if m else ua[:40]


def parse_caddy_json(line: str) -> dict[str, Any] | None:
    """Parse a Caddy JSON access log line.

    Tolerates an optional Docker ``--timestamps`` prefix so the same
    parser works for both file-source and docker-source ``caddy`` log
    groups. Builds a human-readable ``message`` summarising the request
    and lifts ``level`` from the JSON envelope.
    """
    stripped = line.rstrip("\r\n").strip()
    if not stripped:
        return None

    docker_prefix = _DOCKER_TS_RE.match(stripped)
    if docker_prefix is not None:
        stripped = docker_prefix.group(2)

    truncated_line, truncated = _truncate(stripped)
    try:
        parsed = json.loads(truncated_line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    obj: dict[str, Any] = cast(dict[str, Any], parsed)

    # Cert-event branch. certmagic logs lifecycle events under loggers
    # like 'tls.obtain', 'tls.cache.maintenance', etc. They have 'msg'
    # but no 'request'. Pass them through with cert-relevant fields
    # preserved; Storm's _detect_caddy_cert_event classifies which ones
    # are cert_obtained / cert_renewed / cert_failed downstream.
    logger_name = obj.get("logger")
    msg = obj.get("msg")
    if (
        "request" not in obj
        and isinstance(logger_name, str)
        and logger_name.startswith(_CERT_LOGGER_PREFIX)
        and isinstance(msg, str)
        and msg
    ):
        raw_ts = obj.get("ts")
        if raw_ts is None:
            return None
        if isinstance(raw_ts, (int, float)):
            ts_iso = datetime.fromtimestamp(float(raw_ts), tz=UTC).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ",
            )
        else:
            ts_iso = str(raw_ts)
        names = obj.get("names")
        return {
            "ts": ts_iso,
            "level": str(obj.get("level", "info")),
            "message": msg,
            "logger": logger_name,
            "msg": msg,
            "identifier": str(obj.get("identifier") or ""),
            "names": names if isinstance(names, list) else None,
            "error": str(obj.get("error") or ""),
            "truncated": truncated,
        }

    missing = _CADDY_REQUIRED - obj.keys()
    if missing:
        return None

    request_raw = obj.get("request", {})
    if not isinstance(request_raw, dict):
        return None
    request: dict[str, Any] = cast(dict[str, Any], request_raw)

    raw_ts = obj.get("ts")
    if isinstance(raw_ts, (int, float)):
        ts_iso = datetime.fromtimestamp(float(raw_ts), tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ",
        )
    else:
        ts_iso = str(raw_ts)

    method = request.get("method", "") or ""
    uri = request.get("uri", "") or ""
    host = request.get("host", "") or ""
    client_ip = request.get("remote_ip", "") or request.get("client_ip", "") or ""
    status = obj.get("status", 0)

    headers_raw = request.get("headers") or {}
    headers: dict[str, Any] = (
        cast(dict[str, Any], headers_raw) if isinstance(headers_raw, dict) else {}
    )
    ua_list = headers.get("User-Agent") or []
    ua = str(ua_list[0]) if isinstance(ua_list, list) and ua_list else ""
    ua_brief = _summarize_user_agent(ua)

    message = f"{method} {host}{uri} -> {status}"
    if client_ip:
        message += f" from {client_ip}"
    if ua_brief:
        message += f" ({ua_brief})"

    return {
        "ts": ts_iso,
        "level": obj.get("level", "info"),
        "message": message,
        "client_ip": client_ip,
        "method": method,
        "host": host,
        "path": uri,
        "status": status,
        "user_agent": ua,
        "duration_ms": int(float(obj.get("duration", 0.0)) * 1000),
        "bytes_sent": obj.get("size", 0),
        "truncated": truncated,
    }


def parse_docker_raw(line: str) -> dict[str, Any] | None:
    """Parse a Docker ``--timestamps`` log line.

    Docker prefixes each line with an RFC3339Nano timestamp followed by
    whitespace and the container's original log output. Returns ``None``
    when the leading timestamp is missing. ANSI escape sequences in the
    message body are stripped - many containers emit terminal colors
    that are unreadable in a web UI.
    """
    stripped = line.rstrip("\r\n")
    if not stripped:
        return None
    truncated_line, truncated = _truncate(stripped)
    m = _DOCKER_TS_RE.match(truncated_line)
    if m is None:
        return None
    return {
        "ts": m.group(1),
        "message": _ANSI_ESCAPE_RE.sub("", m.group(2)),
        "truncated": truncated,
    }


PARSERS: dict[str, Any] = {
    "garage_s3": parse_garage_s3,
    "stormpulse": parse_stormpulse,
    "caddy_json": parse_caddy_json,
    "docker_raw": parse_docker_raw,
}
