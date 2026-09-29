"""Tests for the CORS commands: the admin-API write, the normalizer, and both
handlers against a stubbed admin client. The stale refusal is the point: a
mismatch never reaches UpdateBucket."""

from __future__ import annotations

import json
from typing import Any

import pytest

from stormpulse.commands.jobs import JobOutcome
from stormpulse.garage import admin_api
from stormpulse.garage.jobs.bucket_cors import (
    cors_rules_shape_problem,
    make_bucket_cors_get_handler,
    make_bucket_cors_set_handler,
    normalize_cors_rules,
)

_ADMIN = {"admin_url": "http://127.0.0.1:3903", "admin_token": "tok"}
_PREFIX = "8742c023e7e97dc8"
_FULL_ID = _PREFIX + "0" * 48

_RULE = {
    "ID": "web",
    "MaxAgeSeconds": 3600,
    "AllowedOrigin": ["https://example.com"],
    "AllowedMethod": ["GET", "PUT"],
    "AllowedHeader": ["*"],
    "ExposeHeader": ["ETag"],
}
_OTHER_RULE = {
    "AllowedOrigin": ["https://other.example"],
    "AllowedMethod": ["GET"],
    "AllowedHeader": [],
    "ExposeHeader": [],
}


class _Progress:
    async def __call__(self, *a: Any, **k: Any) -> None:
        return None


class _FakeAdmin:
    """Stands in for ``admin_api``: one bucket record, a log of every write."""

    def __init__(self, info: dict[str, Any] | None, err: str = "") -> None:
        self.info = info
        self.err = err
        self.writes: list[tuple[str, list[dict[str, Any]]]] = []
        self.write_ok: tuple[bool, str] = (True, "")

    def get_bucket_info(self, **kw: Any) -> tuple[dict[str, Any] | None, str]:
        return self.info, self.err

    def set_bucket_cors(
        self,
        *,
        admin_url: str,
        admin_token: str,
        bucket_id: str,
        rules: list[dict[str, Any]],
    ) -> tuple[bool, str]:
        self.writes.append((bucket_id, rules))
        return self.write_ok


def _install(
    monkeypatch: pytest.MonkeyPatch,
    current: object = None,
    *,
    err: str = "",
) -> _FakeAdmin:
    info = None if err else {"id": _FULL_ID, "corsRules": current}
    fake = _FakeAdmin(info, err)
    monkeypatch.setattr(admin_api, "get_bucket_info", fake.get_bucket_info)
    monkeypatch.setattr(admin_api, "set_bucket_cors", fake.set_bucket_cors)
    return fake


async def _set(
    rules: list[dict[str, Any]],
    expected: list[dict[str, Any]],
) -> JobOutcome:
    handler = make_bucket_cors_set_handler(
        {
            "bucket_id": _PREFIX,
            "rules": json.dumps(rules),
            "expected_rules": json.dumps(expected),
        },
        **_ADMIN,
    )
    assert handler is not None
    return await handler(_Progress())


async def _get() -> JobOutcome:
    handler = make_bucket_cors_get_handler({"bucket_id": _PREFIX}, **_ADMIN)
    assert handler is not None
    return await handler(_Progress())


# ---------------------------------------------------------------------------
# admin_api.set_bucket_cors: the request Garage sees
# ---------------------------------------------------------------------------


def test_set_bucket_cors_posts_cors_rules_to_update_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, bytes | None]] = []

    def fake_request(
        admin_url: str,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes | None = None,
    ) -> tuple[int, str]:
        calls.append((method, path, body))
        return 200, "{}"

    monkeypatch.setattr(admin_api, "_request", fake_request)
    ok, err = admin_api.set_bucket_cors(**_ADMIN, bucket_id=_FULL_ID, rules=[_RULE])
    assert (ok, err) == (True, "")
    assert calls == [
        (
            "POST",
            f"/v2/UpdateBucket?id={_FULL_ID}",
            json.dumps({"corsRules": [_RULE]}).encode(),
        ),
    ]


def test_set_bucket_cors_surfaces_a_rejected_rule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        admin_api,
        "_request",
        lambda *a, **k: (400, '{"message": "missing field `AllowedOrigin`"}'),
    )
    ok, err = admin_api.set_bucket_cors(**_ADMIN, bucket_id=_FULL_ID, rules=[{}])
    assert ok is False
    assert "400" in err and "AllowedOrigin" in err


# ---------------------------------------------------------------------------
# normalize_cors_rules: null and absent optionals equal []
# ---------------------------------------------------------------------------


def test_normalize_fills_absent_and_null_lists_and_drops_null_scalars() -> None:
    sparse = [
        {
            "AllowedOrigin": ["*"],
            "AllowedMethod": ["GET"],
            "ExposeHeader": None,
            "ID": None,
        }
    ]
    assert normalize_cors_rules(sparse) == [
        {
            "AllowedOrigin": ["*"],
            "AllowedMethod": ["GET"],
            "AllowedHeader": [],
            "ExposeHeader": [],
        },
    ]
    assert normalize_cors_rules([]) == []
    assert normalize_cors_rules([_RULE]) == [_RULE]


def test_normalize_keeps_rule_order() -> None:
    # CORS matches the first rule that fits, so order is part of the config.
    assert normalize_cors_rules([_RULE, _OTHER_RULE]) != normalize_cors_rules(
        [_OTHER_RULE, _RULE]
    )


_UNREADABLE = [
    ({"AllowedOrigin": ["*"]}, "is dict, expected a list"),
    (["GET"], "[0] is str, expected a rule object"),
    (
        [{**_RULE, "AllowedOrigins": ["*"]}],
        "[0] carries unknown keys ['AllowedOrigins']",
    ),
]


@pytest.mark.parametrize(("raw", "fragment"), _UNREADABLE)
def test_shape_problem_names_the_type_or_key(raw: object, fragment: str) -> None:
    problem = cors_rules_shape_problem(raw)
    assert problem is not None and fragment in problem
    assert cors_rules_shape_problem([_RULE, _OTHER_RULE]) is None


# ---------------------------------------------------------------------------
# garage_bucket_cors_set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_refuses_stale_expected_rules_and_returns_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch, current=[_OTHER_RULE])
    outcome = await _set(rules=[_RULE], expected=[_RULE])
    assert outcome.success is False
    assert outcome.failure_reason == "cors_rules_stale"
    assert outcome.extras["rules"] == [_OTHER_RULE]
    assert fake.writes == [], "a stale write reached UpdateBucket"


@pytest.mark.asyncio
async def test_set_writes_when_expected_matches_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch, current=[_OTHER_RULE])
    outcome = await _set(rules=[_RULE], expected=[_OTHER_RULE])
    assert outcome.success is True
    assert fake.writes == [(_PREFIX, [_RULE])]
    assert outcome.extras == {
        "bucket_id": _PREFIX,
        "rules": [_RULE],
        "duration_seconds": outcome.extras["duration_seconds"],
    }


@pytest.mark.asyncio
async def test_set_compares_normalized_forms(monkeypatch: pytest.MonkeyPatch) -> None:
    # Garage reads back [] for lists the caller never sent, and the caller may
    # send null: neither is a difference.
    fake = _install(monkeypatch, current=[_OTHER_RULE])
    sparse_expected = [
        {
            "AllowedOrigin": ["https://other.example"],
            "AllowedMethod": ["GET"],
            "ExposeHeader": None,
        }
    ]
    outcome = await _set(rules=[_RULE], expected=sparse_expected)
    assert outcome.success is True, outcome.stderr
    assert len(fake.writes) == 1


@pytest.mark.asyncio
async def test_set_empty_rules_clears_and_absent_cors_reads_as_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A bucket with no CORS config carries no corsRules key; expected [] matches it.
    fake = _install(monkeypatch, current=None)
    outcome = await _set(rules=[], expected=[])
    assert outcome.success is True
    assert fake.writes == [(_PREFIX, [])]
    assert "Cleared" in outcome.stdout


@pytest.mark.asyncio
async def test_set_writes_normalized_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, current=None)
    await _set(
        rules=[{"AllowedOrigin": ["*"], "AllowedMethod": ["GET"], "ID": None}],
        expected=[],
    )
    assert fake.writes[0][1] == [
        {
            "AllowedOrigin": ["*"],
            "AllowedMethod": ["GET"],
            "AllowedHeader": [],
            "ExposeHeader": [],
        },
    ]


@pytest.mark.asyncio
async def test_set_surfaces_a_failed_write(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _install(monkeypatch, current=None)
    fake.write_ok = (False, "HTTP 400: bad rule")
    outcome = await _set(rules=[_RULE], expected=[])
    assert outcome.success is False
    assert outcome.failure_reason == "cors_update_failed"
    assert "400" in outcome.stderr


@pytest.mark.asyncio
@pytest.mark.parametrize(("raw", "fragment"), _UNREADABLE)
async def test_set_and_get_refuse_an_unknown_rules_shape(
    monkeypatch: pytest.MonkeyPatch,
    raw: object,
    fragment: str,
) -> None:
    """A corsRules shape this agent was not written for never reads as [] (which
    would match expected_rules=[] and license an overwrite): it is a refusal."""
    fake = _install(monkeypatch, current=raw)
    for outcome in (await _set(rules=[], expected=[]), await _get()):
        assert outcome.success is False
        assert outcome.failure_reason == "cors_rules_unreadable"
        assert fragment in outcome.stderr
        assert "rules" not in outcome.extras
    assert fake.writes == [], "an unreadable read reached UpdateBucket"


@pytest.mark.asyncio
async def test_set_and_get_name_a_missing_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, err="HTTP 404: NoSuchBucket")
    for outcome in (await _set(rules=[_RULE], expected=[]), await _get()):
        assert outcome.success is False
        assert outcome.failure_reason == "bucket_not_found"


@pytest.mark.asyncio
async def test_set_and_get_surface_a_failed_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install(monkeypatch, err="HTTP 500: boom")
    for outcome in (await _set(rules=[_RULE], expected=[]), await _get()):
        assert outcome.failure_reason == "bucket_info_failed"
    assert fake.writes == []


def test_set_handler_none_on_missing_or_bad_params() -> None:
    good = {"bucket_id": _PREFIX, "rules": "[]", "expected_rules": "[]"}
    for key in good:
        assert (
            make_bucket_cors_set_handler(
                {k: v for k, v in good.items() if k != key}, **_ADMIN
            )
            is None
        )
    assert make_bucket_cors_set_handler({**good, "rules": "not json"}, **_ADMIN) is None
    assert (
        make_bucket_cors_set_handler({**good, "expected_rules": "{}"}, **_ADMIN) is None
    )
    assert make_bucket_cors_set_handler({**good, "rules": '["GET"]'}, **_ADMIN) is None
    assert (
        make_bucket_cors_set_handler({**good, "rules": '[{"Origin": []}]'}, **_ADMIN)
        is None
    )
    assert make_bucket_cors_set_handler(good, admin_url="", admin_token="") is None
    assert make_bucket_cors_set_handler(good, **_ADMIN) is not None


# ---------------------------------------------------------------------------
# garage_bucket_cors_get
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_bucket_id_and_normalized_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, current=[{"AllowedOrigin": ["*"], "AllowedMethod": ["GET"]}])
    outcome = await _get()
    assert outcome.success is True
    assert outcome.extras["bucket_id"] == _PREFIX
    assert outcome.extras["rules"] == [
        {
            "AllowedOrigin": ["*"],
            "AllowedMethod": ["GET"],
            "AllowedHeader": [],
            "ExposeHeader": [],
        },
    ]


@pytest.mark.asyncio
async def test_get_reads_no_config_as_an_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, current=None)
    outcome = await _get()
    assert outcome.success is True
    assert outcome.extras["rules"] == []


def test_get_handler_none_on_missing_bucket_or_unconfigured_admin() -> None:
    assert make_bucket_cors_get_handler({}, **_ADMIN) is None
    assert (
        make_bucket_cors_get_handler(
            {"bucket_id": _PREFIX}, admin_url="", admin_token=""
        )
        is None
    )


def test_normalize_keeps_a_zero_max_age() -> None:
    # 0 means "do not cache the preflight", a real value; only null/absent drop.
    rule = {"AllowedOrigin": ["*"], "AllowedMethod": ["GET"], "MaxAgeSeconds": 0}
    assert normalize_cors_rules([rule])[0]["MaxAgeSeconds"] == 0
