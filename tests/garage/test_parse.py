"""Tests for stormpulse.garage.parse — pure parsers, no subprocess."""

from __future__ import annotations

import pytest

from stormpulse.garage.parse import (
    GarageParseError,
    parse_bucket_info,
    parse_bucket_list,
    parse_key_create,
    parse_key_list,
    parse_stats,
    parse_status,
)
from tests.garage.fixtures import (
    BUCKET_INFO_OUTPUT,
    BUCKET_INFO_OUTPUT_NO_KEYS,
    BUCKET_LIST_OUTPUT,
    BUCKET_LIST_OUTPUT_EMPTY,
    BUCKET_LIST_OUTPUT_MULTI,
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
# garage bucket list
# ---------------------------------------------------------------------------


class TestParseBucketList:
    def test_single_bucket(self) -> None:
        buckets = parse_bucket_list(BUCKET_LIST_OUTPUT)
        assert len(buckets) == 1
        assert buckets[0].bucket_id == "f1dc32249aa1d80a"
        assert buckets[0].global_alias == "obsidian-vault"

    def test_multi_bucket(self) -> None:
        buckets = parse_bucket_list(BUCKET_LIST_OUTPUT_MULTI)
        assert len(buckets) == 2
        assert buckets[1].global_alias == "backups"

    def test_empty_list(self) -> None:
        buckets = parse_bucket_list(BUCKET_LIST_OUTPUT_EMPTY)
        assert buckets == []

    def test_blank_string(self) -> None:
        buckets = parse_bucket_list("")
        assert buckets == []


# ---------------------------------------------------------------------------
# garage bucket info
# ---------------------------------------------------------------------------


class TestParseBucketInfo:
    def test_bucket_with_key(self) -> None:
        info = parse_bucket_info(BUCKET_INFO_OUTPUT)
        assert info.bucket_id.startswith("f1dc3224")
        assert info.size_bytes == 5800  # 5.8 KB
        assert info.object_count == 2
        assert info.website_access is False
        assert info.global_alias == "obsidian-vault"
        assert len(info.keys) == 1
        assert info.keys[0].permissions == "RWO"
        assert info.keys[0].access_key_id == "GK5e6fb0b4fa406ace8126a7db"
        assert info.keys[0].local_alias == "obsidian-key"

    def test_bucket_no_keys(self) -> None:
        info = parse_bucket_info(BUCKET_INFO_OUTPUT_NO_KEYS)
        assert info.object_count == 0
        assert info.keys == []
        assert info.global_alias == "empty-bucket"

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
