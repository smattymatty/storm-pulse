"""Tests for the admin-API write/mutation client (ADR garage/001).

These back the provisioning migration off the Garage CLI: ``create_key``,
``allow_bucket_key`` / ``deny_bucket_key``, ``add_bucket_alias_local``, and
``delete_key``. As in the read tests, we patch the single transport
(``admin_api._request``) and assert the request shape (path, method, JSON body)
and the failure mapping, so a bad write surfaces as ``(False/None, error)``,
never an exception.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from stormpulse.garage import admin_api

_ADMIN = {"admin_url": "http://127.0.0.1:3903", "admin_token": "tok"}
_PREFIX = "8742c023e7e97dc8"  # Storm's 16-char garage_bucket_id
_FULL_ID = _PREFIX + "0" * 48  # 64 chars
_KEY_ID = "GK31c2f6a8b9d04e15f7c3a2b1"


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    resolve_id: str | None = _FULL_ID,
    op_status: int = 200,
    op_body: str = "{}",
) -> list[dict[str, Any]]:
    """Patch ``admin_api._request`` with a recorder.

    A ``GetBucketInfo`` path (the full-id resolve) answers with ``resolve_id``;
    any other path answers ``op_status``/``op_body``. Returns the call log.
    ``resolve_id=None`` simulates a prefix that matches no bucket.
    """
    calls: list[dict[str, Any]] = []
    resolve_body = json.dumps({"id": resolve_id}) if resolve_id else json.dumps({})

    def _request(
        admin_url: str,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes | None = None,
    ) -> tuple[int | None, str]:
        calls.append({"method": method, "path": path, "body": body})
        if "GetBucketInfo" in path:
            return 200, resolve_body
        return op_status, op_body

    monkeypatch.setattr(admin_api, "_request", _request)
    return calls


def _body_of(call: dict[str, Any]) -> dict[str, Any]:
    assert call["body"] is not None
    return json.loads(call["body"])


class TestCreateKey:
    def test_posts_name_and_returns_secret(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = _install(
            monkeypatch,
            op_body=json.dumps(
                {"accessKeyId": _KEY_ID, "secretAccessKey": "s3cr3t", "name": "usr-1"}
            ),
        )
        info, err = admin_api.create_key(name="usr-1", **_ADMIN)
        assert err == ""
        assert info is not None
        assert info["accessKeyId"] == _KEY_ID
        assert info["secretAccessKey"] == "s3cr3t"
        assert calls[-1]["method"] == "POST"
        assert calls[-1]["path"] == "/v2/CreateKey"
        assert _body_of(calls[-1]) == {"name": "usr-1"}

    def test_http_error_maps_to_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, op_status=500, op_body="boom")
        info, err = admin_api.create_key(name="usr-1", **_ADMIN)
        assert info is None
        assert "HTTP 500" in err


class TestAllowBucketKey:
    def test_rw_resolves_then_grants_read_write(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = _install(monkeypatch)
        ok, err = admin_api.allow_bucket_key(
            bucket_ref=_PREFIX, access_key_id=_KEY_ID, read=True, write=True, **_ADMIN
        )
        assert (ok, err) == (True, "")
        # First call resolves the prefix; second grants.
        assert "GetBucketInfo" in calls[0]["path"]
        assert calls[-1]["method"] == "POST"
        assert calls[-1]["path"] == "/v2/AllowBucketKey"
        assert _body_of(calls[-1]) == {
            "bucketId": _FULL_ID,
            "accessKeyId": _KEY_ID,
            "permissions": {"read": True, "write": True, "owner": False},
        }

    def test_ro_grants_read_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _install(monkeypatch)
        ok, _ = admin_api.allow_bucket_key(
            bucket_ref=_PREFIX, access_key_id=_KEY_ID, read=True, write=False, **_ADMIN
        )
        assert ok is True
        assert _body_of(calls[-1])["permissions"] == {
            "read": True,
            "write": False,
            "owner": False,
        }

    def test_unresolvable_prefix_skips_grant(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = _install(monkeypatch, resolve_id=None)
        ok, err = admin_api.allow_bucket_key(
            bucket_ref=_PREFIX, access_key_id=_KEY_ID, read=True, write=True, **_ADMIN
        )
        assert ok is False
        assert err
        # Only the resolve was attempted; no AllowBucketKey POST went out.
        assert all("AllowBucketKey" not in c["path"] for c in calls)


class TestDenyBucketKey:
    def test_posts_deny_with_permissions(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = _install(monkeypatch)
        ok, err = admin_api.deny_bucket_key(
            bucket_ref=_PREFIX, access_key_id=_KEY_ID, read=True, write=True, **_ADMIN
        )
        assert (ok, err) == (True, "")
        assert calls[-1]["path"] == "/v2/DenyBucketKey"
        assert _body_of(calls[-1]) == {
            "bucketId": _FULL_ID,
            "accessKeyId": _KEY_ID,
            "permissions": {"read": True, "write": True, "owner": False},
        }


class TestAddBucketAliasLocal:
    def test_posts_local_alias_triple(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = _install(monkeypatch)
        ok, err = admin_api.add_bucket_alias_local(
            bucket_ref=_PREFIX, access_key_id=_KEY_ID, local_alias="data", **_ADMIN
        )
        assert (ok, err) == (True, "")
        assert calls[-1]["path"] == "/v2/AddBucketAlias"
        assert _body_of(calls[-1]) == {
            "bucketId": _FULL_ID,
            "localAlias": "data",
            "accessKeyId": _KEY_ID,
        }


class TestDeleteKey:
    def test_deletes_by_id_query_param(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = _install(monkeypatch)
        ok, err = admin_api.delete_key(access_key_id=_KEY_ID, **_ADMIN)
        assert (ok, err) == (True, "")
        assert calls[-1]["method"] == "POST"
        assert calls[-1]["path"] == f"/v2/DeleteKey?id={_KEY_ID}"
        assert calls[-1]["body"] is None

    def test_http_error_maps_to_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _install(monkeypatch, op_status=404, op_body="no such key")
        ok, err = admin_api.delete_key(access_key_id=_KEY_ID, **_ADMIN)
        assert ok is False
        assert "HTTP 404" in err
