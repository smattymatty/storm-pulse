"""What each log parser declares it emits, per parser and per variant.

Published as ``log-line-contract.json`` at the repo root, and **separate from
``wire-contract.json`` on purpose.** That artifact's digest is advertised at
register, and a consumer that sees a mismatch may refuse destructive work. A
logging-field change must never be able to pause that work, so log-line shapes
do not share that file, that digest, or that blast radius. Nothing here is
advertised at runtime.

**Declarations are explicit, never inferred from the functions.** The
declaration is the contract; the parser is the implementation. A contract
derived from the implementation cannot detect the implementation being wrong,
it can only restate it. ``tests/logging/test_log_line_contract.py`` proves the
two agree by running each parser on its golden line and comparing key sets, so
a parser that stops emitting a declared field fails this repo's own suite
before any consumer sees the artifact.

Why this exists
---------------
A consumer once began reading a key from a ``caddy_json`` line that no parser
here emits. Nothing failed: the agent kept shipping, the consumer kept
ingesting, and the field simply arrived empty forever. Both sides were
individually reasonable, both test suites stayed green, and static analysis saw
a tested helper and correctly called it referenced.

The failure is only visible where the two meet, which is nowhere either
repository looks. This module is the agent's half of making it visible: a
declaration of what is actually emitted, in a form a consumer can assert
against in CI.

Adding a parser, or a variant
-----------------------------
Add its golden line and its declared fields together, run ``make
log-line-contract``, and review the diff as a change to a published contract.
If the test fails, the declaration and the parser disagree: fix whichever is
wrong, never the assertion.
"""

from __future__ import annotations

import json
from typing import Any

SCHEMA = 1

# Fields the SHIPPER adds after the parser returns, per parser. These are part
# of what a consumer receives and are invisible to the parser functions, so a
# declaration built only from parser output would understate the wire.
# ``LogShipper.collect`` stamps bucket_id on garage_s3 lines only (its
# ``_BUCKET_ID_PARSER``); other groups carry no bucket name and no consumer
# reads it for them.
SHIPPER_ADDED: dict[str, tuple[str, ...]] = {
    "garage_s3": ("bucket_id",),
}

# One representative raw line per parser variant. Real captured shapes, not
# invented ones: an invented sample can pass a parser while missing the branch
# a real line takes. The test runs the parser on each of these.
GOLDEN_LINES: dict[str, dict[str, str]] = {
    "garage_s3": {
        "request": (
            "2026-04-10T13:23:51.766230Z  INFO garage_api_common::generic_server: "
            "71.19.243.102 (via [::1]:37780) (key GKc8a2eafe464b4754187172d0) "
            "HEAD /usr-1-obsidian-vault/some/key.txt"
        ),
    },
    "caddy_json": {
        "access": json.dumps({
            "ts": "2026-04-10T13:00:00Z", "status": 200, "duration": 0.015,
            "size": 1024,
            "request": {
                "remote_ip": "1.2.3.4", "method": "GET", "uri": "/bucket/key",
                "host": "alpha.example.ca",
                "headers": {"User-Agent": ["aws-cli/2"]},
            },
        }),
        "cert_event": json.dumps({
            "level": "info", "ts": 1776000000.0, "logger": "tls.obtain",
            "msg": "certificate obtained successfully", "identifier": "spike.test",
        }),
    },
    "stormpulse": {
        "line": json.dumps({
            "ts": "2026-04-10T13:00:00Z", "level": "WARNING",
            "message": "agent reconnecting", "event_type": "ws_reconnect",
        }),
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
        "line": json.dumps({
            "__REALTIME_TIMESTAMP": "1776000000000000",
            "MESSAGE": "guard boot: ENFORCE mode, policy generation 96",
            "PRIORITY": "6",
            "_SYSTEMD_UNIT": "storm-buckets-guard.service",
        }),
    },
}

# What each variant emits. Sorted, so the artifact diff stays readable.
#
# Read this before adding a consumer: a field absent here is a field this agent
# does not send, whatever any document on the other side says. `caddy_json`
# carries `path` and NO `bucket`; deriving a bucket from that path is the
# consumer's job or a future parser change, and either way it is a change to
# this file first.
DECLARED: dict[str, dict[str, tuple[str, ...]]] = {
    "garage_s3": {
        "request": (
            "bucket", "client_ip", "key_id", "level", "message", "method",
            "object_key", "path", "proxy", "response_code", "truncated", "ts",
        ),
    },
    "caddy_json": {
        "access": (
            "bytes_sent", "client_ip", "duration_ms", "host", "level",
            "message", "method", "path", "status", "truncated", "ts",
            "user_agent",
        ),
        "cert_event": (
            "error", "identifier", "level", "logger", "message", "msg",
            "names", "truncated", "ts",
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
    """Per-parser declared shape, as the artifact carries it."""
    return {
        parser: {
            "variants": {
                variant: sorted(fields)
                for variant, fields in sorted(variants.items())
            },
            "shipper_added": sorted(SHIPPER_ADDED.get(parser, ())),
            # The union a consumer actually asserts against, precomputed so
            # every consumer does not re-derive it. Redundant with the two
            # keys above by construction, and deliberately so: the rule
            # "variants OR-ed together, plus shipper_added" is easy to get
            # subtly wrong, and forgetting the shipper half is exactly how a
            # reader would miss `bucket_id`.
            "all_fields": sorted(emitted_fields(parser)),
        }
        for parser, variants in sorted(DECLARED.items())
    }


def emitted_fields(parser: str) -> set[str]:
    """Every field a consumer may see for ``parser``, across all variants.

    The union, because a consumer reading a field only one variant carries is
    correct (it handles the absence); a consumer reading a field NO variant
    carries is the defect this module exists to make visible.
    """
    fields: set[str] = set(SHIPPER_ADDED.get(parser, ()))
    for variant_fields in DECLARED.get(parser, {}).values():
        fields.update(variant_fields)
    return fields


def build_log_line_contract() -> dict[str, Any]:
    """The full artifact. No digest: this is not advertised at register, and a
    number nobody compares is a moving part with no reader."""
    return {"schema": SCHEMA, "parsers": log_line_wire_shape()}


def render_log_line_contract() -> str:
    """The artifact as the exact text the checked-in file holds."""
    return json.dumps(
        build_log_line_contract(), indent=2, sort_keys=True, ensure_ascii=True
    ) + "\n"
