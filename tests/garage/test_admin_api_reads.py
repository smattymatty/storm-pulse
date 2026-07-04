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


class TestClusterReads:
    def test_get_cluster_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        req, seen = _fake_request(200, json.dumps({"nodes": [{"id": "n1"}]}))
        monkeypatch.setattr(admin_api, "_request", req)
        data, err = admin_api.get_cluster_status(**_ADMIN)
        assert err == ""
        assert data == {"nodes": [{"id": "n1"}]}
        assert seen["path"] == "/v2/GetClusterStatus"

    def test_get_cluster_statistics(self, monkeypatch: pytest.MonkeyPatch) -> None:
        req, seen = _fake_request(200, json.dumps({"totalObjectCount": 7032}))
        monkeypatch.setattr(admin_api, "_request", req)
        data, err = admin_api.get_cluster_statistics(**_ADMIN)
        assert err == ""
        assert data is not None and data["totalObjectCount"] == 7032
        assert seen["path"] == "/v2/GetClusterStatistics"

    def test_list_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        req, seen = _fake_request(200, json.dumps([{"id": "GK1", "name": "k"}]))
        monkeypatch.setattr(admin_api, "_request", req)
        data, err = admin_api.list_keys(**_ADMIN)
        assert err == ""
        assert data == [{"id": "GK1", "name": "k"}]
        assert seen["path"] == "/v2/ListKeys"

    def test_list_keys_non_list_is_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        req, _seen = _fake_request(200, json.dumps({"oops": 1}))
        monkeypatch.setattr(admin_api, "_request", req)
        data, err = admin_api.list_keys(**_ADMIN)
        assert data is None
        assert "non-list" in err

    def test_get_key_info_returns_buckets(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        body = json.dumps({"accessKeyId": "GK1", "buckets": [{"id": "b1"}]})
        req, seen = _fake_request(200, body)
        monkeypatch.setattr(admin_api, "_request", req)
        data, err = admin_api.get_key_info(access_key_id="GK1", **_ADMIN)
        assert err == ""
        assert data is not None and data["buckets"] == [{"id": "b1"}]
        assert seen["path"] == "/v2/GetKeyInfo?id=GK1"


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


class TestEventTarget:
    """Admin-call events attribute their target resource by endpoint
    family, from the ?id= query param (events answer "which bucket or
    key was this call about")."""

    def test_bucket_endpoint_maps_to_bucket_id(self) -> None:
        out = admin_api._event_target("UpdateBucket", "/v2/UpdateBucket?id=abc123")
        assert out == {"bucket_id": "abc123"}

    def test_key_endpoint_maps_to_key_id(self) -> None:
        out = admin_api._event_target("GetKeyInfo", "/v2/GetKeyInfo?id=GK1")
        assert out == {"key_id": "GK1"}

    def test_idless_and_unfamiliar_endpoints_contribute_nothing(self) -> None:
        assert admin_api._event_target("ListBuckets", "/v2/ListBuckets") == {}
        assert admin_api._event_target("GetClusterStatus", "/v2/GetClusterStatus") == {}
        assert admin_api._event_target("GetNodeInfo", "/v2/GetNodeInfo?id=n1") == {}
