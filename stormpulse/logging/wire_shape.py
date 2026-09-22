"""Explicit parser fields published as ``log-line-contract.json`` for consumer CI.

Kept separate from ``wire-contract.json`` and its runtime digest so logging
changes cannot block destructive work. Golden-line tests verify declarations
against parser output, catching missing or unexpected fields.

For new parsers or variants, add a golden line and declared fields, then run
``make log-line-contract``. Review the diff; fix mismatches, not assertions.
"""

from __future__ import annotations

import json
from typing import Any

SCHEMA = 1

# Fields added after parsing by LogShipper.collect, included in the contract.
# Only garage_s3 receives bucket_id.
SHIPPER_ADDED: dict[str, tuple[str, ...]] = {
    "garage_s3": ("bucket_id",),
}

# Golden test lines use real output shapes with synthetic values.
# These fixtures ship in every wheel; never include customer data.
GOLDEN_LINES: dict[str, dict[str, str]] = {
    "garage_s3": {
        "request": (
            "2026-04-10T13:23:51.766230Z  INFO garage_api_common::generic_server: "
            "192.0.2.10 (via [::1]:37780) (key GKdeadbeef1234567890abcdef) "
            "HEAD /usr-0-example-bucket/some/key.txt"
        ),
    },
    "caddy_json": {
        "access": json.dumps(
            {
                "ts": "2026-04-10T13:00:00Z",
                "status": 200,
                "duration": 0.015,
                "size": 1024,
                "request": {
                    "remote_ip": "1.2.3.4",
                    "method": "GET",
                    "uri": "/bucket/key",
                    "host": "alpha.example.ca",
                    "headers": {"User-Agent": ["aws-cli/2"]},
                },
            }
        ),
        "cert_event": json.dumps(
            {
                "level": "info",
                "ts": 1776000000.0,
                "logger": "tls.obtain",
                "msg": "certificate obtained successfully",
                "identifier": "spike.test",
            }
        ),
    },
    "stormpulse": {
        "line": json.dumps(
            {
                "ts": "2026-04-10T13:00:00Z",
                "level": "WARNING",
                "message": "agent reconnecting",
                "event_type": "ws_reconnect",
            }
        ),
    },
    "docker_raw": {
        "line": "2026-04-10T13:00:00.000000000Z hello from the container",
    },
    "django": {
        "app_line": (
            "2026-04-10 13:00:00,000 developer.pulse WARNING Adopt: bucket thing"
        ),
    },
    "journald": {
        "line": json.dumps(
            {
                "__REALTIME_TIMESTAMP": "1776000000000000",
                "MESSAGE": "guard boot: ENFORCE mode, policy generation 96",
                "PRIORITY": "6",
                "_SYSTEMD_UNIT": "storm-buckets-guard.service",
            }
        ),
    },
}

# Declared parser fields, sorted for readable artifact diffs.
# caddy_json emits path, not bucket; consumers must derive the bucket.
# Parser field changes require updating these declarations.
DECLARED: dict[str, dict[str, tuple[str, ...]]] = {
    "garage_s3": {
        "request": (
            "bucket",
            "client_ip",
            "key_id",
            "level",
            "message",
            "method",
            "object_key",
            "path",
            "proxy",
            "response_code",
            "truncated",
            "ts",
        ),
    },
    "caddy_json": {
        "access": (
            "bytes_sent",
            "client_ip",
            "duration_ms",
            "host",
            "level",
            "message",
            "method",
            "path",
            "status",
            "truncated",
            "ts",
            "user_agent",
        ),
        "cert_event": (
            "error",
            "identifier",
            "level",
            "logger",
            "message",
            "msg",
            "names",
            "truncated",
            "ts",
        ),
    },
    "stormpulse": {
        "line": ("event_type", "level", "message", "truncated", "ts"),
    },
    "docker_raw": {
        "line": ("message", "truncated", "ts"),
    },
    "django": {
        "app_line": ("level", "logger", "message", "truncated", "ts"),
    },
    "journald": {
        "line": ("message", "truncated", "ts"),
    },
}


def log_line_wire_shape() -> dict[str, Any]:
    """Return each parser's declared fields in artifact format."""
    return {
        parser: {
            "variants": {
                variant: sorted(fields) for variant, fields in sorted(variants.items())
            },
            "shipper_added": sorted(SHIPPER_ADDED.get(parser, ())),
            # Combine variants and shipper fields so consumers include bucket_id.
            "all_fields": sorted(emitted_fields(parser)),
        }
        for parser, variants in sorted(DECLARED.items())
    }


def emitted_fields(parser: str) -> set[str]:
    """Return all possible parser and shipper fields across variants.

    Consumers must handle fields absent from individual variants.
    """
    fields: set[str] = set(SHIPPER_ADDED.get(parser, ()))
    for variant_fields in DECLARED.get(parser, {}).values():
        fields.update(variant_fields)
    return fields


def build_log_line_contract() -> dict[str, Any]:
    """Build the contract without a digest; registration does not advertise it."""
    return {"schema": SCHEMA, "parsers": log_line_wire_shape()}


def render_log_line_contract() -> str:
    """Render the contract in the checked-in artifact's format."""
    return (
        json.dumps(
            build_log_line_contract(), indent=2, sort_keys=True, ensure_ascii=True
        )
        + "\n"
    )
