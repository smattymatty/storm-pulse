"""Strict per-format parsers for log lines.

Each parser takes one raw line and returns a parsed dict or ``None``
if the line is invalid, malformed, or unrecognised. Parsers NEVER
eval, exec, or interpret line content — they extract fields via
regex or structured JSON only.
"""

from __future__ import annotations

import json
import re
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


def parse_garage_s3(line: str) -> dict[str, Any] | None:
    """Parse a Garage S3 access log line.

    Returns ``None`` for non-matching lines, admin API noise, or
    anything that doesn't look like a customer-facing request.
    """
    if "garage_api_admin" in line:
        return None

    stripped = line.rstrip("\r\n")
    truncated_line, truncated = _truncate(stripped)
    m = _GARAGE_S3_RE.fullmatch(truncated_line)
    if m is None:
        return None

    path = m.group("path")
    # bucket is the first path component; object_key is the rest
    bucket = ""
    object_key = ""
    if path.startswith("/"):
        # strip query string for bucket/object extraction
        path_part = path.split("?", 1)[0]
        parts = path_part[1:].split("/", 1)
        bucket = parts[0]
        object_key = parts[1] if len(parts) > 1 else ""

    return {
        "ts": m.group("ts"),
        "client_ip": m.group("ip"),
        "proxy": m.group("proxy"),
        "key_id": m.group("key_id"),
        "method": m.group("method"),
        "path": path,
        "bucket": bucket,
        "object_key": object_key,
        "response_code": None,
        "truncated": truncated,
    }


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


def parse_caddy_json(line: str) -> dict[str, Any] | None:
    """Parse a Caddy JSON access log line.

    Stub: returns ``None`` for now — the ``network`` group is disabled
    by default. This function exists so config validation accepts the
    ``caddy_json`` parser value and the shipper can route to it.
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

    missing = _CADDY_REQUIRED - obj.keys()
    if missing:
        return None

    request: dict[str, Any] = obj.get("request", {})
    if not isinstance(request, dict):
        return None

    return {
        "ts": obj["ts"],
        "client_ip": request.get("remote_ip", ""),
        "method": request.get("method", ""),
        "path": request.get("uri", ""),
        "status": obj["status"],
        "duration_ms": int(float(obj.get("duration", 0.0)) * 1000),
        "bytes_sent": obj.get("size", 0),
        "truncated": truncated,
    }


_DOCKER_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+(.*)$")


def parse_docker_raw(line: str) -> dict[str, Any] | None:
    """Parse a Docker ``--timestamps`` log line.

    Docker prefixes each line with an RFC3339Nano timestamp followed by
    whitespace and the container's original log output. Returns ``None``
    when the leading timestamp is missing.
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
        "message": m.group(2),
        "truncated": truncated,
    }


PARSERS: dict[str, Any] = {
    "garage_s3": parse_garage_s3,
    "stormpulse": parse_stormpulse,
    "caddy_json": parse_caddy_json,
    "docker_raw": parse_docker_raw,
}
