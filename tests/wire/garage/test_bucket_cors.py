"""CORS rules against a real Garage: the JSON shape the agent parses and sends.

The fakes prove the agent sends ``corsRules`` and compares normalized forms;
only this proves Garage stores what was sent and reads it back under the same
names, which is what the stale compare rests on.
Run: ``make garage-up && make test-garage-wire``
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from stormpulse.commands.jobs import JobOutcome
from stormpulse.garage import admin_api
from stormpulse.garage.jobs.bucket_cors import (
    make_bucket_cors_get_handler,
    make_bucket_cors_set_handler,
)
from tests.wire.garage.conftest import WireEnv, garage_cli, pretty, unique_alias

RULE_KEYS = {
    "ID",
    "MaxAgeSeconds",
    "AllowedOrigin",
    "AllowedMethod",
    "AllowedHeader",
    "ExposeHeader",
}
_RULE = {
    "ID": "web",
    "MaxAgeSeconds": 3600,
    "AllowedOrigin": ["https://example.com"],
    "AllowedMethod": ["GET", "PUT"],
    "AllowedHeader": ["*"],
    "ExposeHeader": ["ETag"],
}


class _Progress:
    async def __call__(self, *a: Any, **k: Any) -> None:
        return None


@pytest.fixture
def cors_bucket(wire: WireEnv) -> Iterator[str]:
    """A bucket made and removed through the admin API; S3 is never touched."""
    alias = unique_alias("cors")
    created, err = admin_api.create_bucket(**wire.admin_kwargs, global_alias=alias)
    assert err == "", err
    assert created is not None
    try:
        yield created["id"]
    finally:
        garage_cli("bucket", "delete", "--yes", alias)


async def _set(
    wire: WireEnv,
    bucket_id: str,
    rules: list[dict[str, Any]],
    expected: list[dict[str, Any]],
) -> JobOutcome:
    handler = make_bucket_cors_set_handler(
        {
            "bucket_id": bucket_id,
            "rules": json.dumps(rules),
            "expected_rules": json.dumps(expected),
        },
        **wire.admin_kwargs,
    )
    assert handler is not None
    return await handler(_Progress())


async def _get(wire: WireEnv, bucket_id: str) -> JobOutcome:
    handler = make_bucket_cors_get_handler(
        {"bucket_id": bucket_id}, **wire.admin_kwargs
    )
    assert handler is not None
    return await handler(_Progress())


@pytest.mark.asyncio
async def test_rules_round_trip_under_the_xml_names(
    wire: WireEnv, cors_bucket: str
) -> None:
    """Set through the command, read back through the command: same keys, same
    values. A renamed or re-cased key here breaks the stale compare fleet-wide."""
    fresh = await _get(wire, cors_bucket)
    assert fresh.success, fresh.stderr
    assert fresh.extras["rules"] == [], pretty(fresh.extras)

    written = await _set(wire, cors_bucket, rules=[_RULE], expected=[])
    assert written.success, written.stderr

    read = await _get(wire, cors_bucket)
    assert read.success, read.stderr
    rules = read.extras["rules"]
    assert len(rules) == 1, pretty(rules)
    assert set(rules[0]) == RULE_KEYS, pretty(rules)
    assert rules[0] == _RULE, pretty(rules)

    raw, err = admin_api.get_bucket_info(**wire.admin_kwargs, bucket_ref=cors_bucket)
    assert err == "", err
    assert raw is not None
    assert raw.get("corsRules") == [_RULE], pretty(raw.get("corsRules"))


@pytest.mark.asyncio
async def test_a_sparse_rule_reads_back_with_empty_lists(
    wire: WireEnv, cors_bucket: str
) -> None:
    """Garage fills ``AllowedHeader``/``ExposeHeader`` with ``[]`` and leaves
    ``ID``/``MaxAgeSeconds`` out: the normalizer's contract."""
    sparse = {"AllowedOrigin": ["*"], "AllowedMethod": ["GET"]}
    written = await _set(wire, cors_bucket, rules=[sparse], expected=[])
    assert written.success, written.stderr
    raw, err = admin_api.get_bucket_info(**wire.admin_kwargs, bucket_ref=cors_bucket)
    assert err == "", err
    assert raw is not None
    assert raw.get("corsRules") == [
        {
            "AllowedOrigin": ["*"],
            "AllowedMethod": ["GET"],
            "AllowedHeader": [],
            "ExposeHeader": [],
        },
    ], pretty(raw.get("corsRules"))


@pytest.mark.asyncio
async def test_stale_expected_rules_are_refused_live(
    wire: WireEnv, cors_bucket: str
) -> None:
    """Rules changed under the caller: the write is refused and the live rules
    come back; Garage still holds the first write."""
    first = await _set(wire, cors_bucket, rules=[_RULE], expected=[])
    assert first.success, first.stderr

    stale = await _set(wire, cors_bucket, rules=[], expected=[])
    assert stale.success is False
    assert stale.failure_reason == "cors_rules_stale"
    assert stale.extras["rules"] == [_RULE], pretty(stale.extras)

    read = await _get(wire, cors_bucket)
    assert read.extras["rules"] == [_RULE], pretty(read.extras)


@pytest.mark.asyncio
async def test_empty_rules_clear_the_config(wire: WireEnv, cors_bucket: str) -> None:
    """``[]`` removes the CORS config; Garage then reports no ``corsRules`` at all."""
    first = await _set(wire, cors_bucket, rules=[_RULE], expected=[])
    assert first.success, first.stderr
    cleared = await _set(wire, cors_bucket, rules=[], expected=[_RULE])
    assert cleared.success, cleared.stderr

    raw, err = admin_api.get_bucket_info(**wire.admin_kwargs, bucket_ref=cors_bucket)
    assert err == "", err
    assert raw is not None
    assert "corsRules" not in raw, pretty(raw)
    read = await _get(wire, cors_bucket)
    assert read.extras["rules"] == [], pretty(read.extras)


@pytest.mark.asyncio
async def test_a_bucket_id_prefix_resolves_on_both_commands(
    wire: WireEnv, cors_bucket: str
) -> None:
    """The dispatcher may send the 16-hex prefix, never only the full id: the
    write resolves it before UpdateBucket and the read searches by it."""
    prefix = cors_bucket[:16]
    written = await _set(wire, prefix, rules=[_RULE], expected=[])
    assert written.success, written.stderr
    read = await _get(wire, prefix)
    assert read.success, read.stderr
    assert read.extras["rules"] == [_RULE], pretty(read.extras)
    raw, err = admin_api.get_bucket_info(**wire.admin_kwargs, bucket_ref=cors_bucket)
    assert err == "", err
    assert raw is not None and raw.get("corsRules") == [_RULE], pretty(raw)
