"""Tests for the admin-API read client (ADR garage/001 follow-up #1).

``list_buckets`` and ``get_bucket_info`` are the state read loop's grounded
JSON path. We patch the single transport (``admin_api._request``) and assert the
request shape (``?id=`` vs ``?search=``), the prefix-match guard, and the
failure mapping to ``(None, error)`` so a bad read never raises.
"""
from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from stormpulse.garage import admin_api

_ADMIN = {"admin_url": "http://127.0.0.1:3903", "admin_token": "tok"}
_PREFIX = "8742c023e7e97dc8"
_FULL_ID = _PREFIX + "0" * 48

_RequestFn = Callable[..., tuple[int | None, str]]


def _fake_request(
    status: int | None, body: str,
) -> tuple[_RequestFn, dict[str, str]]:
    """Build a drop-in for admin_api._request that records the path it saw."""
    seen: dict[str, str] = {}

    def _request(
        admin_url: str,
        method: str,
        path: str,
        headers: dict[str, str],
        body_bytes: bytes | None = None,
    ) -> tuple[int | None, str]:
        seen["method"] = method
        seen["path"] = path
        return status, body

    return _request, seen


class TestGetBucketInfo:
    def test_full_id_uses_id_param(self, monkeypatch: pytest.MonkeyPatch) -> None:
        req, seen = _fake_request(200, json.dumps({"id": _FULL_ID, "bytes": 5}))
        monkeypatch.setattr(admin_api, "_request", req)
        info, err = admin_api.get_bucket_info(bucket_ref=_FULL_ID, **_ADMIN)
        assert err == ""
        assert info == {"id": _FULL_ID, "bytes": 5}
        assert f"id={_FULL_ID}" in seen["path"]
        assert "search=" not in seen["path"]

    def test_prefix_uses_search_and_verifies_match(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        req, seen = _fake_request(200, json.dumps({"id": _FULL_ID, "bytes": 9}))
        monkeypatch.setattr(admin_api, "_request", req)
        info, err = admin_api.get_bucket_info(bucket_ref=_PREFIX, **_ADMIN)
        assert err == ""
        assert info is not None and info["bytes"] == 9
        assert f"search={_PREFIX}" in seen["path"]

    def test_prefix_mismatch_is_rejected(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # search returned a bucket whose id does NOT start with the prefix.
        req, _ = _fake_request(200, json.dumps({"id": "ffff" + "0" * 60}))
        monkeypatch.setattr(admin_api, "_request", req)
        info, err = admin_api.get_bucket_info(bucket_ref=_PREFIX, **_ADMIN)
        assert info is None
        assert "does not match" in err

    def test_non_2xx_returns_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        req, _ = _fake_request(500, "boom")
        monkeypatch.setattr(admin_api, "_request", req)
        info, err = admin_api.get_bucket_info(bucket_ref=_FULL_ID, **_ADMIN)
        assert info is None
        assert "HTTP 500" in err

    def test_unreachable_returns_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # _request signals "couldn't connect" with status=None, message in body.
        def req(
            admin_url: str,
            method: str,
            path: str,
            headers: dict[str, str],
            body_bytes: bytes | None = None,
        ) -> tuple[int | None, str]:
            return None, "Could not reach Garage admin API"

        monkeypatch.setattr(admin_api, "_request", req)
        info, err = admin_api.get_bucket_info(bucket_ref=_FULL_ID, **_ADMIN)
        assert info is None
        assert "Could not reach" in err

    def test_non_json_returns_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        req, _ = _fake_request(200, "<html>not json</html>")
        monkeypatch.setattr(admin_api, "_request", req)
        info, err = admin_api.get_bucket_info(bucket_ref=_FULL_ID, **_ADMIN)
        assert info is None
        assert "non-JSON" in err


class TestListBuckets:
    def test_returns_dict_items(self, monkeypatch: pytest.MonkeyPatch) -> None:
        body = json.dumps([{"id": _FULL_ID, "globalAliases": ["a"]}, {"id": "x"}])
        req, seen = _fake_request(200, body)
        monkeypatch.setattr(admin_api, "_request", req)
        items, err = admin_api.list_buckets(**_ADMIN)
        assert err == ""
        assert items == [{"id": _FULL_ID, "globalAliases": ["a"]}, {"id": "x"}]
        assert seen["path"] == "/v2/ListBuckets"

    def test_non_list_body_is_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        req, _ = _fake_request(200, json.dumps({"not": "a list"}))
        monkeypatch.setattr(admin_api, "_request", req)
        items, err = admin_api.list_buckets(**_ADMIN)
        assert items is None
        assert "non-list" in err

    def test_unreachable_returns_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def req(
            admin_url: str,
            method: str,
            path: str,
            headers: dict[str, str],
            body_bytes: bytes | None = None,
        ) -> tuple[int | None, str]:
            return None, "Could not reach Garage admin API"

        monkeypatch.setattr(admin_api, "_request", req)
        items, err = admin_api.list_buckets(**_ADMIN)
        assert items is None
        assert "Could not reach" in err
