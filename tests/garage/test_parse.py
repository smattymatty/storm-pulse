"""Tests for stormpulse.garage.parse - pure parsers, no subprocess."""

from __future__ import annotations

import pytest

from stormpulse.garage.parse import (
    GarageParseError,
    parse_bucket_info,
    parse_key_create,
    parse_key_list,
    parse_stats,
    parse_status,
)
from tests.garage.fixtures import (
    BUCKET_INFO_OUTPUT,
    BUCKET_INFO_OUTPUT_NO_KEYS,
    BUCKET_INFO_OUTPUT_QUOTA_SIZE_ONLY,
    BUCKET_INFO_OUTPUT_WEBSITE_CUSTOM_ERROR,
    BUCKET_INFO_OUTPUT_WEBSITE_ENABLED,
    BUCKET_INFO_OUTPUT_WITH_QUOTAS,
    KEY_CREATE_OUTPUT,
    KEY_LIST_OUTPUT,
    KEY_LIST_OUTPUT_EMPTY,
    KEY_LIST_OUTPUT_MULTI,
    STATS_OUTPUT,
    STATS_OUTPUT_EMPTY,
    STATUS_OUTPUT,
    STATUS_OUTPUT_EMPTY,
    STATUS_OUTPUT_MULTI_NODE,
)

# ---------------------------------------------------------------------------
# garage status
# ---------------------------------------------------------------------------


class TestParseStatus:
    def test_single_healthy_node(self) -> None:
        nodes = parse_status(STATUS_OUTPUT)
        assert len(nodes) == 1
        n = nodes[0]
        assert n.node_id == "7a58a5fa192ad6dd"
        assert n.hostname == "garage-one"
        assert n.address == "127.0.0.1:3901"
        assert n.zone == "canada-1"
        assert n.capacity_gb == 10.0
        assert n.data_avail_gb == 16.3
        assert n.data_avail_percent == 83.0
        assert n.version == "v2.2.0"
        assert n.healthy is True

    def test_multi_node(self) -> None:
        nodes = parse_status(STATUS_OUTPUT_MULTI_NODE)
        assert len(nodes) == 3
        assert nodes[0].node_id == "7a58a5fa192ad6dd"
        assert nodes[0].zone == "canada-1"
        assert nodes[1].node_id == "ab12cd34ef56gh78"
        assert nodes[1].zone == "ca-east-2"
        assert nodes[1].capacity_gb == 20.0
        assert nodes[2].node_id == "cd34ef56gh78ij90"
        assert nodes[2].zone == "ca-home-1"
        assert all(n.healthy for n in nodes)

    def test_empty_output(self) -> None:
        nodes = parse_status(STATUS_OUTPUT_EMPTY)
        assert nodes == []

    def test_blank_string(self) -> None:
        nodes = parse_status("")
        assert nodes == []


# ---------------------------------------------------------------------------
# garage stats
# ---------------------------------------------------------------------------


class TestParseStats:
    def test_normal_output(self) -> None:
        stats = parse_stats(STATS_OUTPUT)
        assert stats.db_engine == "sqlite3 v3.50.2 (using rusqlite crate)"
        assert stats.object_count == 5
        assert stats.block_count == 1

    def test_minimal_output(self) -> None:
        stats = parse_stats(STATS_OUTPUT_EMPTY)
        assert stats.db_engine == "unknown"
        assert stats.object_count == 0
        assert stats.block_count == 0

    def test_blank_string(self) -> None:
        stats = parse_stats("")
        assert stats.db_engine == "unknown"
        assert stats.object_count == 0


# ---------------------------------------------------------------------------
# garage bucket info
# ---------------------------------------------------------------------------


class TestParseBucketInfo:
    def test_bucket_info_v2_format_with_key_name_column(self) -> None:
        """REGRESSION: Garage v2.2.0's ``bucket info`` keys table has
        4 columns (Permissions | Access key | Key name | Local aliases),
        not the 3-column shape the fixture assumes. Without column-
        count-aware parsing, the parser reads parts[2] (the key name)
        as the local alias - which then gets passed to
        ``bucket unalias --local <key> <name>`` and Garage rejects with
        ``No bucket called <key-name> in namespace of key <key>`` because
        the actual alias is in parts[3]. Empirically observed on
        garage-one (v2.2.0).
        """
        from stormpulse.garage.parse import parse_bucket_info

        output = (
            "==== BUCKET INFORMATION ====\n"
            "Bucket:          5c8d6c0bb73f0770e5a7a7d471c0d434fb9b37098017d66f40ee3300a040bc3c\n"
            "Created:         2026-05-08 00:00:00.000 +00:00\n"
            "Size:            0 B\n"
            "Objects:         0\n"
            "Website access:  false\n"
            "==== KEYS FOR THIS BUCKET ====\n"
            "Permissions  Access key                  Key name              Local aliases\n"
            "RWO          GK8559f761a01bf7716d6aa3d8  usr-1-obsidian-all    obsidian\n"
        )
        info = parse_bucket_info(output)
        assert len(info.keys) == 1
        assert info.keys[0].permissions == "RWO"
        assert info.keys[0].access_key_id == "GK8559f761a01bf7716d6aa3d8"
        assert info.keys[0].local_alias == "obsidian"

    def test_bucket_with_key(self) -> None:
        info = parse_bucket_info(BUCKET_INFO_OUTPUT)
        assert info.bucket_id.startswith("f1dc3224")
        assert info.size_bytes == 5800  # 5.8 KB
        assert info.object_count == 2
        assert info.website_access is False
        assert info.website_index_document == "index.html"
        assert info.website_error_document is None
        assert info.global_alias == "obsidian-vault"
        assert len(info.keys) == 1
        assert info.keys[0].permissions == "RWO"
        assert info.keys[0].access_key_id == "GK5e6fb0b4fa406ace8126a7db"
        assert info.keys[0].local_alias == "obsidian-key"
        assert info.quota_max_size_bytes is None
        assert info.quota_max_objects is None

    def test_bucket_no_keys(self) -> None:
        info = parse_bucket_info(BUCKET_INFO_OUTPUT_NO_KEYS)
        assert info.object_count == 0
        assert info.keys == []
        assert info.global_alias == "empty-bucket"
        assert info.quota_max_size_bytes is None
        assert info.quota_max_objects is None

    def test_bucket_with_both_quotas(self) -> None:
        info = parse_bucket_info(BUCKET_INFO_OUTPUT_WITH_QUOTAS)
        assert info.bucket_id.startswith("f1dc3224")
        assert info.quota_max_size_bytes == 1_000_000_000  # 1000.0 MB
        assert info.quota_max_objects == 1000

    def test_bucket_with_size_quota_only(self) -> None:
        info = parse_bucket_info(BUCKET_INFO_OUTPUT_QUOTA_SIZE_ONLY)
        assert info.quota_max_size_bytes == 1_000_000_000
        assert info.quota_max_objects is None

    def test_website_enabled_no_error_doc(self) -> None:
        info = parse_bucket_info(BUCKET_INFO_OUTPUT_WEBSITE_ENABLED)
        assert info.website_access is True
        assert info.website_index_document == "index.html"
        assert info.website_error_document is None

    def test_website_custom_error_document(self) -> None:
        info = parse_bucket_info(BUCKET_INFO_OUTPUT_WEBSITE_CUSTOM_ERROR)
        assert info.website_access is True
        assert info.website_index_document == "index.html"
        assert info.website_error_document == "404.html"

    def test_invalid_output_raises(self) -> None:
        with pytest.raises(GarageParseError, match="bucket ID"):
            parse_bucket_info("nothing useful here")


# ---------------------------------------------------------------------------
# garage key list
# ---------------------------------------------------------------------------


class TestParseKeyList:
    def test_single_key(self) -> None:
        keys = parse_key_list(KEY_LIST_OUTPUT)
        assert len(keys) == 1
        assert keys[0].key_id == "GK5e6fb0b4fa406ace8126a7db"
        assert keys[0].name == "obsidian-key"

    def test_multi_key(self) -> None:
        keys = parse_key_list(KEY_LIST_OUTPUT_MULTI)
        assert len(keys) == 2
        assert keys[1].name == "backup-key"

    def test_empty_list(self) -> None:
        keys = parse_key_list(KEY_LIST_OUTPUT_EMPTY)
        assert keys == []


# ---------------------------------------------------------------------------
# garage key create
# ---------------------------------------------------------------------------


class TestParseKeyCreate:
    def test_normal_output(self) -> None:
        result = parse_key_create(KEY_CREATE_OUTPUT)
        assert result.key_id == "GKdeadbeef1234567890abcdef"
        assert result.name == "test-key"
        assert result.secret_key == "REDACTED_SECRET_DO_NOT_LOG_abcdefghijklmnop"

    def test_missing_secret_raises(self) -> None:
        with pytest.raises(GarageParseError, match="secret"):
            parse_key_create("Key name: test\nKey ID: GK123\n")

    def test_missing_key_id_raises(self) -> None:
        with pytest.raises(GarageParseError, match="key ID"):
            parse_key_create("Key name: test\nSecret key: abc\n")
