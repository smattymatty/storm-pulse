"""Tests for stormpulse.garage.state.

All telemetry (cluster status, statistics, key list) and per-bucket state are
read via the admin HTTP API (ADR garage/001), so every test mocks the
``admin_api`` functions and asserts against exact-integer JSON. No CLI.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

from stormpulse.garage.config import GarageConfig
from stormpulse.garage.state import collect_garage_state

ADMIN_URL = "http://127.0.0.1:3903"
# A full 64-char Garage bucket id; Storm stores its first 16 chars.
FULL_ID = "f1dc32249aa1d80a" + "0" * 48
FULL_ID_2 = "abcdef9876543210" + "1" * 48
NODE_ID = "a8bfb94f8a2786f74c227c75a690846b915560c08dc8a0c8681b980082d0a4b9"


def _make_config(tmp_path: Path, *, admin_url: str = ADMIN_URL) -> GarageConfig:
    return GarageConfig(
        enabled=True,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=tmp_path / "garage.toml",
        admin_url=admin_url,
        admin_token="tok",
    )


def _node(
    *,
    node_id: str = NODE_ID,
    hostname: str = "garage-one",
    zone: str = "canada-1",
    version: str = "v2.3.0",
    up: bool = True,
    capacity: int | None = 3_000_000_000_000,
    avail: int = 2_800_000_000_000,
    total: int = 3_000_000_000_000,
) -> dict[str, Any]:
    """Build a NodeResp-shaped dict (GetClusterStatus v2)."""
    return {
        "id": node_id,
        "hostname": hostname,
        "addr": "10.0.0.1:3901",
        "garageVersion": version,
        "isUp": up,
        "role": {"zone": zone, "capacity": capacity, "tags": []},
        "dataPartition": {"available": avail, "total": total},
    }


def _admin_key(
    access_key_id: str,
    name: str,
    *,
    read: bool = True,
    write: bool = True,
    owner: bool = True,
    local_aliases: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "accessKeyId": access_key_id,
        "name": name,
        "permissions": {"read": read, "write": write, "owner": owner},
        "bucketLocalAliases": list(local_aliases),
    }


def _admin_info(
    full_id: str,
    *,
    bytes_: int,
    objects: int,
    global_aliases: tuple[str, ...] = (),
    keys: tuple[dict[str, Any], ...] = (),
    website: dict[str, Any] | None = None,
    max_size: int | None = None,
    max_objects: int | None = None,
) -> dict[str, Any]:
    """Build a GetBucketInfoResponse-shaped dict (admin API v2)."""
    return {
        "id": full_id,
        "globalAliases": list(global_aliases),
        "websiteAccess": website is not None,
        "websiteConfig": website,
        "keys": list(keys),
        "objects": objects,
        "bytes": bytes_,
        "quotas": {"maxSize": max_size, "maxObjects": max_objects},
    }


_StatusResult = tuple[dict[str, Any] | None, str]
_StatsResult = tuple[dict[str, Any] | None, str]
_KeysResult = tuple[list[dict[str, Any]] | None, str]
_ListResult = tuple[list[dict[str, Any]] | None, str]

_STATUS_OK: _StatusResult = ({"nodes": [_node()]}, "")
_STATS_OK: _StatsResult = ({"totalObjectCount": 5, "bucketCount": 1}, "")
_KEYS_OK: _KeysResult = ([{"id": "GK5e6fb0b4fa406ace8126a7db", "name": "obsidian-key"}], "")


@contextmanager
def _patched(
    *,
    status: _StatusResult = _STATUS_OK,
    stats: _StatsResult = _STATS_OK,
    keys: _KeysResult = _KEYS_OK,
    list_result: _ListResult,
    info_by_id: Mapping[str, tuple[dict[str, Any] | None, str]],
) -> Iterator[None]:
    """Patch the five admin reads the state collector uses."""

    def info_side_effect(
        *, admin_url: str, admin_token: str, bucket_ref: str,
    ) -> tuple[dict[str, Any] | None, str]:
        return info_by_id.get(bucket_ref, (None, f"not found: {bucket_ref}"))

    with (
        patch("stormpulse.garage.state.admin_api.get_cluster_status", return_value=status),
        patch("stormpulse.garage.state.admin_api.get_cluster_statistics", return_value=stats),
        patch("stormpulse.garage.state.admin_api.list_keys", return_value=keys),
        patch("stormpulse.garage.state.admin_api.list_buckets", return_value=list_result),
        patch("stormpulse.garage.state.admin_api.get_bucket_info", side_effect=info_side_effect),
    ):
        yield


class TestCollectGarageState:
    def test_full_collection(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        info = _admin_info(
            FULL_ID,
            bytes_=5800,
            objects=2,
            global_aliases=("obsidian-vault",),
            keys=(_admin_key("GK5e6fb0b4fa406ace8126a7db", "obsidian-key"),),
        )
        with _patched(list_result=([{"id": FULL_ID}], ""), info_by_id={FULL_ID: (info, "")}):
            state = collect_garage_state(cfg)

        assert state is not None
        # Node telemetry from GetClusterStatus.
        assert state.node_id == NODE_ID
        assert state.hostname == "garage-one"
        assert state.zone == "canada-1"
        assert state.version == "v2.3.0"
        assert state.healthy is True
        # object_count from GetClusterStatistics.totalObjectCount.
        assert state.object_count == 5
        # Bucket from GetBucketInfo, exact integers.
        bucket = state.buckets[0]
        assert bucket.id == FULL_ID
        assert bucket.alias == "obsidian-vault"
        assert bucket.size_bytes == 5800
        assert bucket.object_count == 2
        assert bucket.keys[0].key_id == "GK5e6fb0b4fa406ace8126a7db"
        assert bucket.keys[0].key_name == "obsidian-key"
        assert bucket.keys[0].permissions == "RWO"
        # Top-level key inventory from ListKeys.
        assert len(state.keys) == 1
        assert state.keys[0].key_id == "GK5e6fb0b4fa406ace8126a7db"
        assert state.keys[0].key_name == "obsidian-key"
        assert state.keys[0].permissions == ""
        # Peers from GetClusterStatus, with exact byte->GB conversion.
        assert len(state.peers) == 1
        peer = state.peers[0]
        assert peer.node_id == NODE_ID
        assert peer.hostname == "garage-one"
        assert peer.version == "v2.3.0"
        assert peer.capacity_gb == 3000.0
        assert peer.data_avail_gb == 2800.0
        assert peer.data_avail_percent == 93.3

    def test_bucket_with_quotas(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        info = _admin_info(
            FULL_ID, bytes_=10, objects=1, max_size=1_000_000_000, max_objects=1000,
        )
        with _patched(list_result=([{"id": FULL_ID}], ""), info_by_id={FULL_ID: (info, "")}):
            state = collect_garage_state(cfg)

        assert state is not None
        bucket = state.buckets[0]
        assert bucket.quota_max_size_bytes == 1_000_000_000
        assert bucket.quota_max_objects == 1000

    def test_permission_flags_partial(self, tmp_path: Path) -> None:
        """Structured {read,write,owner} renders back to the legacy RWO flags."""
        cfg = _make_config(tmp_path)
        info = _admin_info(
            FULL_ID,
            bytes_=0,
            objects=0,
            keys=(
                _admin_key("GKread", "ro-key", read=True, write=False, owner=False),
                _admin_key("GKrw", "rw-key", read=True, write=True, owner=False),
            ),
        )
        with _patched(list_result=([{"id": FULL_ID}], ""), info_by_id={FULL_ID: (info, "")}):
            state = collect_garage_state(cfg)

        assert state is not None
        perms = {k.key_id: k.permissions for k in state.buckets[0].keys}
        assert perms == {"GKread": "R", "GKrw": "RW"}

    def test_s3_created_bucket_surfaces_owner_local_alias(
        self, tmp_path: Path,
    ) -> None:
        """A BUCKETS-012 bucket created over S3 has no global alias; its name
        lives in the owning account key's local aliases, which the manifest
        must carry so the website's adopt branch can name it."""
        cfg = _make_config(tmp_path)
        info = _admin_info(
            FULL_ID,
            bytes_=0,
            objects=0,
            global_aliases=(),  # S3-created: no global alias
            keys=(
                _admin_key(
                    "GKacct", "acct-key", owner=True, local_aliases=("media",),
                ),
            ),
        )
        with _patched(list_result=([{"id": FULL_ID}], ""), info_by_id={FULL_ID: (info, "")}):
            state = collect_garage_state(cfg)

        assert state is not None
        bucket = state.buckets[0]
        assert bucket.alias == ""
        assert bucket.keys[0].bucket_local_aliases == ("media",)

    def test_key_without_local_aliases_defaults_empty(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        info = _admin_info(
            FULL_ID,
            bytes_=0,
            objects=0,
            keys=(_admin_key("GKplain", "plain-key"),),
        )
        with _patched(list_result=([{"id": FULL_ID}], ""), info_by_id={FULL_ID: (info, "")}):
            state = collect_garage_state(cfg)

        assert state is not None
        assert state.buckets[0].keys[0].bucket_local_aliases == ()

    def test_website_config_mapped(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        info = _admin_info(
            FULL_ID,
            bytes_=0,
            objects=0,
            website={"indexDocument": "home.html", "errorDocument": "404.html"},
        )
        with _patched(list_result=([{"id": FULL_ID}], ""), info_by_id={FULL_ID: (info, "")}):
            state = collect_garage_state(cfg)

        assert state is not None
        bucket = state.buckets[0]
        assert bucket.website_access is True
        assert bucket.website_index_document == "home.html"
        assert bucket.website_error_document == "404.html"

    def test_alias_less_bucket_collected(self, tmp_path: Path) -> None:
        """Buckets with no global alias still collect; alias is empty, id is full."""
        cfg = _make_config(tmp_path)
        info = _admin_info(FULL_ID, bytes_=5800, objects=2)
        with _patched(list_result=([{"id": FULL_ID}], ""), info_by_id={FULL_ID: (info, "")}):
            state = collect_garage_state(cfg)

        assert state is not None
        assert state.buckets[0].id == FULL_ID
        assert state.buckets[0].alias == ""

    def test_multi_bucket_all_collected(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        infos = {
            FULL_ID: (_admin_info(FULL_ID, bytes_=1, objects=1, global_aliases=("a",)), ""),
            FULL_ID_2: (_admin_info(FULL_ID_2, bytes_=2, objects=2, global_aliases=("b",)), ""),
        }
        with _patched(
            list_result=([{"id": FULL_ID}, {"id": FULL_ID_2}], ""), info_by_id=infos,
        ):
            state = collect_garage_state(cfg)

        assert state is not None
        assert {b.alias for b in state.buckets} == {"a", "b"}

    def test_get_bucket_info_failure_skips_one_bucket(self, tmp_path: Path) -> None:
        """A single bucket whose GetBucketInfo fails is skipped, not fatal."""
        cfg = _make_config(tmp_path)
        infos = {
            FULL_ID: (_admin_info(FULL_ID, bytes_=1, objects=1, global_aliases=("ok",)), ""),
            FULL_ID_2: (None, "HTTP 500"),
        }
        with _patched(
            list_result=([{"id": FULL_ID}, {"id": FULL_ID_2}], ""), info_by_id=infos,
        ):
            state = collect_garage_state(cfg)

        assert state is not None
        assert [b.alias for b in state.buckets] == ["ok"]

    def test_list_buckets_failure_skips_tick(self, tmp_path: Path) -> None:
        """ListBuckets unreachable -> None (skip the whole push, don't report empty)."""
        cfg = _make_config(tmp_path)
        with _patched(list_result=(None, "Could not reach admin API"), info_by_id={}):
            assert collect_garage_state(cfg) is None

    def test_admin_unconfigured_skips_tick(self, tmp_path: Path) -> None:
        """No admin_url/admin_token -> skip the tick before any admin call."""
        cfg = _make_config(tmp_path, admin_url="")
        assert collect_garage_state(cfg) is None

    def test_empty_cluster_no_buckets(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with _patched(list_result=([], ""), info_by_id={}):
            state = collect_garage_state(cfg)
        assert state is not None
        assert state.buckets == []

    def test_status_failure_returns_none(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with _patched(status=(None, "unreachable"), list_result=([], ""), info_by_id={}):
            assert collect_garage_state(cfg) is None

    def test_no_nodes_returns_none(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with _patched(status=({"nodes": []}, ""), list_result=([], ""), info_by_id={}):
            assert collect_garage_state(cfg) is None

    def test_stats_failure_still_returns_state(self, tmp_path: Path) -> None:
        """A GetClusterStatistics failure degrades object_count to 0, not the tick."""
        cfg = _make_config(tmp_path)
        with _patched(stats=(None, "HTTP 500"), list_result=([], ""), info_by_id={}):
            state = collect_garage_state(cfg)
        assert state is not None
        assert state.object_count == 0
        assert state.buckets == []

    def test_key_list_failure_empties_top_level_not_bucket_keys(
        self, tmp_path: Path,
    ) -> None:
        """A ListKeys failure empties the top-level inventory, but bucket keys
        survive because GetBucketInfo carries their names inline."""
        cfg = _make_config(tmp_path)
        info = _admin_info(
            FULL_ID,
            bytes_=1,
            objects=1,
            global_aliases=("vault",),
            keys=(_admin_key("GK5e6fb0b4fa406ace8126a7db", "obsidian-key"),),
        )
        with _patched(
            keys=(None, "HTTP 500"),
            list_result=([{"id": FULL_ID}], ""),
            info_by_id={FULL_ID: (info, "")},
        ):
            state = collect_garage_state(cfg)
        assert state is not None
        assert state.keys == []
        assert state.buckets[0].keys[0].key_name == "obsidian-key"

    def test_multi_node_all_peers_collected(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        status = (
            {
                "nodes": [
                    _node(node_id=NODE_ID, hostname="garage-one"),
                    _node(node_id="b" * 64, hostname="garage-two"),
                    _node(node_id="c" * 64, hostname="garage-pi"),
                ]
            },
            "",
        )
        with _patched(status=status, list_result=([], ""), info_by_id={}):
            state = collect_garage_state(cfg)
        assert state is not None
        assert len(state.peers) == 3
        assert {p.hostname for p in state.peers} == {
            "garage-one", "garage-two", "garage-pi",
        }
        assert state.node_id == NODE_ID

    def test_unlinked_keys_appear_in_top_level(self, tmp_path: Path) -> None:
        """Keys in ListKeys but not on any bucket still surface at top level."""
        cfg = _make_config(tmp_path)
        keys = (
            [
                {"id": "GK5e6fb0b4fa406ace8126a7db", "name": "obsidian-key"},
                {"id": "GKbackup0000000000000000", "name": "backup-key"},
            ],
            "",
        )
        info = _admin_info(
            FULL_ID,
            bytes_=1,
            objects=1,
            global_aliases=("vault",),
            keys=(_admin_key("GK5e6fb0b4fa406ace8126a7db", "obsidian-key"),),
        )
        with _patched(
            keys=keys, list_result=([{"id": FULL_ID}], ""), info_by_id={FULL_ID: (info, "")},
        ):
            state = collect_garage_state(cfg)
        assert state is not None
        assert len(state.buckets[0].keys) == 1
        assert {k.key_name for k in state.keys} == {"obsidian-key", "backup-key"}

    def test_to_dict(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        info = _admin_info(
            FULL_ID, bytes_=5800, objects=2, global_aliases=("obsidian-vault",),
        )
        with _patched(list_result=([{"id": FULL_ID}], ""), info_by_id={FULL_ID: (info, "")}):
            state = collect_garage_state(cfg)

        assert state is not None
        d = state.to_dict()
        assert d["node_id"] == NODE_ID
        assert d["version"] == "v2.3.0"
        assert "db_engine" not in d
        assert "block_count" not in d
        assert d["buckets"][0]["alias"] == "obsidian-vault"
        assert d["buckets"][0]["size_bytes"] == 5800
        assert len(d["peers"]) == 1
        assert d["peers"][0]["node_id"] == NODE_ID
