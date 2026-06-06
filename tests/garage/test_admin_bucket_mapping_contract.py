"""Schema-drift contract test for the admin-API bucket mapping (ADR garage/001).

After moving bucket reads from CLI text to admin-API JSON, the new fragility is
the *field contract* with Garage v2: `_bucket_from_admin_info` reads `bytes`,
`objects`, `quotas.maxSize`, `keys[].accessKeyId`, etc. by exact name. If a Garage
upgrade renames or restructures one, our defensive `.get()` silently degrades to
zero - wrong customer data, no error. This pins a full, realistic
`GetBucketInfoResponse` (the v2.3.0 OpenAPI shape) as a golden fixture and asserts
both the mapping and the presence of every depended-on field path.

To make this a true golden file, replace ``GOLDEN`` with a real response captured
from the live node:
    curl -s -H "Authorization: Bearer $TOKEN" \
      "http://127.0.0.1:3903/v2/GetBucketInfo?id=<full-id>" | python -m json.tool
If a future Garage version changes the shape, re-capturing surfaces the diff and
this test fails until the mapping is updated.
"""
from __future__ import annotations

from typing import Any

from stormpulse.garage.state import _bucket_from_admin_info

_FULL_ID = "f1dc32249aa1d80a" + "0" * 48

# A complete GetBucketInfoResponse per the Garage admin API v2.3.0 OpenAPI schema,
# including fields we do NOT read (created, unfinishedMultipart*, corsRules, ...)
# to prove the mapping ignores extras instead of choking on them.
GOLDEN: dict[str, Any] = {
    "id": _FULL_ID,
    "created": "2026-04-07T12:00:00.000Z",
    "globalAliases": ["obsidian-vault"],
    "websiteAccess": True,
    "websiteConfig": {
        "indexDocument": "index.html",
        "errorDocument": "404.html",
        "routingRules": None,
    },
    "keys": [
        {
            "accessKeyId": "GK5e6fb0b4fa406ace8126a7db",
            "name": "obsidian-key",
            "permissions": {"read": True, "write": True, "owner": True},
            "bucketLocalAliases": ["obsidian"],
        },
        {
            "accessKeyId": "GKreadonly000000000000000",
            "name": "ro-key",
            "permissions": {"read": True, "write": False, "owner": False},
            "bucketLocalAliases": [],
        },
    ],
    "objects": 42,
    "bytes": 5_800_000_000,
    "unfinishedUploads": 0,
    "unfinishedMultipartUploads": 0,
    "unfinishedMultipartUploadParts": 0,
    "unfinishedMultipartUploadBytes": 0,
    "quotas": {"maxSize": 10_000_000_000, "maxObjects": None},
    "corsRules": None,
    "lifecycleRules": None,
}


class TestGoldenMapping:
    def test_maps_every_field(self) -> None:
        b = _bucket_from_admin_info(GOLDEN)
        assert b.id == _FULL_ID
        assert b.alias == "obsidian-vault"
        assert b.size_bytes == 5_800_000_000
        assert b.object_count == 42
        assert b.website_access is True
        assert b.website_index_document == "index.html"
        assert b.website_error_document == "404.html"
        assert b.quota_max_size_bytes == 10_000_000_000
        assert b.quota_max_objects is None
        assert [(k.key_id, k.key_name, k.permissions) for k in b.keys] == [
            ("GK5e6fb0b4fa406ace8126a7db", "obsidian-key", "RWO"),
            ("GKreadonly000000000000000", "ro-key", "R"),
        ]

    def test_null_website_config_defaults(self) -> None:
        g = dict(GOLDEN)
        g["websiteAccess"] = False
        g["websiteConfig"] = None
        b = _bucket_from_admin_info(g)
        assert b.website_access is False
        assert b.website_index_document == "index.html"
        assert b.website_error_document is None

    def test_null_quota_maps_to_none(self) -> None:
        g = dict(GOLDEN)
        g["quotas"] = {"maxSize": None, "maxObjects": None}
        b = _bucket_from_admin_info(g)
        assert b.quota_max_size_bytes is None
        assert b.quota_max_objects is None


class TestFieldContract:
    """Assert every field path the mapping depends on exists with the right shape.

    This is the drift tripwire: a Garage rename (e.g. ``bytes`` -> ``sizeBytes``)
    makes one of these fail, forcing the mapping to be updated rather than
    silently returning zeros.
    """

    def test_top_level_paths(self) -> None:
        assert isinstance(GOLDEN["id"], str)
        assert isinstance(GOLDEN["bytes"], int)
        assert isinstance(GOLDEN["objects"], int)
        assert isinstance(GOLDEN["websiteAccess"], bool)
        assert isinstance(GOLDEN["globalAliases"], list)

    def test_quota_paths(self) -> None:
        assert "maxSize" in GOLDEN["quotas"]
        assert "maxObjects" in GOLDEN["quotas"]

    def test_website_config_paths(self) -> None:
        assert "indexDocument" in GOLDEN["websiteConfig"]
        assert "errorDocument" in GOLDEN["websiteConfig"]

    def test_key_paths(self) -> None:
        key = GOLDEN["keys"][0]
        for field in ("accessKeyId", "name", "permissions", "bucketLocalAliases"):
            assert field in key, field
        for flag in ("read", "write", "owner"):
            assert flag in key["permissions"], flag
