"""Tests for stormpulse.garage.state.

Node telemetry (status, stats, key list, peers) is collected via the Garage CLI
(``run_garage``, mocked here). Per-bucket state (sizes, objects, quotas, keys) is
collected via the admin HTTP API (ADR garage/001 follow-up #1), so the bucket
tests mock ``admin_api.list_buckets`` / ``admin_api.get_bucket_info`` and assert
against exact-integer JSON rather than scraped CLI text.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from unittest.mock import patch

from stormpulse.config import GarageConfig
from stormpulse.garage.parse import GarageParseError
from stormpulse.garage.state import collect_garage_state, run_garage
from tests.garage.fixtures import (
    KEY_LIST_OUTPUT,
    KEY_LIST_OUTPUT_MULTI,
    STATS_OUTPUT,
    STATUS_OUTPUT,
    STATUS_OUTPUT_EMPTY,
    STATUS_OUTPUT_MULTI_NODE,
)

ADMIN_URL = "http://127.0.0.1:3903"
# A full 64-char Garage bucket id; Storm stores its first 16 chars.
FULL_ID = "f1dc32249aa1d80a" + "0" * 48
FULL_ID_2 = "abcdef9876543210" + "1" * 48


def _make_config(tmp_path: Path, *, admin_url: str = ADMIN_URL) -> GarageConfig:
    return GarageConfig(
        enabled=True,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=tmp_path / "garage.toml",
        state_push_interval_seconds=300,
        admin_url=admin_url,
        admin_token="tok",
    )


def _mock_run_garage(outputs: Mapping[tuple[str, ...], str | None]) -> object:
    """side_effect for run_garage that maps CLI args to canned stdout."""

    def side_effect(config: GarageConfig, *args: str) -> str | None:
        return outputs.get(args)

    return side_effect


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


@contextmanager
def _patched(
    cli_outputs: Mapping[tuple[str, ...], str | None],
    list_result: tuple[list[dict[str, Any]] | None, str],
    info_by_id: Mapping[str, tuple[dict[str, Any] | None, str]],
) -> Iterator[None]:
    """Patch CLI (run_garage) + admin reads (list_buckets/get_bucket_info)."""

    def info_side_effect(
        *, admin_url: str, admin_token: str, bucket_ref: str,
    ) -> tuple[dict[str, Any] | None, str]:
        return info_by_id.get(bucket_ref, (None, f"not found: {bucket_ref}"))

    with (
        patch(
            "stormpulse.garage.state.run_garage",
            side_effect=_mock_run_garage(cli_outputs),
        ),
        patch(
            "stormpulse.garage.state.admin_api.list_buckets",
            return_value=list_result,
        ),
        patch(
            "stormpulse.garage.state.admin_api.get_bucket_info",
            side_effect=info_side_effect,
        ),
    ):
        yield


_CLI_OK = {
    ("status",): STATUS_OUTPUT,
    ("stats",): STATS_OUTPUT,
    ("key", "list"): KEY_LIST_OUTPUT,
}


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
        with _patched(_CLI_OK, ([{"id": FULL_ID}], ""), {FULL_ID: (info, "")}):
            state = collect_garage_state(cfg)

        assert state is not None
        # Node telemetry still from the CLI.
        assert state.node_id == "7a58a5fa192ad6dd"
        assert state.hostname == "garage-one"
        assert state.zone == "canada-1"
        assert state.healthy is True
        assert state.db_engine == "sqlite3 v3.50.2 (using rusqlite crate)"
        assert state.object_count == 5
        assert state.block_count == 1
        # Bucket from the admin API, exact integers.
        assert len(state.buckets) == 1
        bucket = state.buckets[0]
        assert bucket.id == FULL_ID
        assert bucket.alias == "obsidian-vault"
        assert bucket.size_bytes == 5800
        assert bucket.object_count == 2
        assert len(bucket.keys) == 1
        assert bucket.keys[0].key_id == "GK5e6fb0b4fa406ace8126a7db"
        # key_name now comes inline from GetBucketInfo, not the key-list map.
        assert bucket.keys[0].key_name == "obsidian-key"
        assert bucket.keys[0].permissions == "RWO"
        assert bucket.website_access is False
        assert bucket.website_index_document == "index.html"
        assert bucket.website_error_document is None
        assert bucket.quota_max_size_bytes is None
        assert bucket.quota_max_objects is None
        # Top-level key inventory still from `key list`.
        assert len(state.keys) == 1
        assert state.keys[0].key_id == "GK5e6fb0b4fa406ace8126a7db"
        assert state.keys[0].key_name == "obsidian-key"
        assert state.keys[0].permissions == ""
        # Peers from garage status.
        assert len(state.peers) == 1
        assert state.peers[0].node_id == "7a58a5fa192ad6dd"
        assert state.peers[0].hostname == "garage-one"

    def test_bucket_with_quotas(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        info = _admin_info(
            FULL_ID, bytes_=10, objects=1, max_size=1_000_000_000, max_objects=1000,
        )
        with _patched(_CLI_OK, ([{"id": FULL_ID}], ""), {FULL_ID: (info, "")}):
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
        with _patched(_CLI_OK, ([{"id": FULL_ID}], ""), {FULL_ID: (info, "")}):
            state = collect_garage_state(cfg)

        assert state is not None
        perms = {k.key_id: k.permissions for k in state.buckets[0].keys}
        assert perms == {"GKread": "R", "GKrw": "RW"}

    def test_website_config_mapped(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        info = _admin_info(
            FULL_ID,
            bytes_=0,
            objects=0,
            website={"indexDocument": "home.html", "errorDocument": "404.html"},
        )
        with _patched(_CLI_OK, ([{"id": FULL_ID}], ""), {FULL_ID: (info, "")}):
            state = collect_garage_state(cfg)

        assert state is not None
        bucket = state.buckets[0]
        assert bucket.website_access is True
        assert bucket.website_index_document == "home.html"
        assert bucket.website_error_document == "404.html"

    def test_alias_less_bucket_collected(self, tmp_path: Path) -> None:
        """Buckets with no global alias still collect; alias is empty, id is full."""
        cfg = _make_config(tmp_path)
        info = _admin_info(FULL_ID, bytes_=5800, objects=2)  # no global aliases
        with _patched(_CLI_OK, ([{"id": FULL_ID}], ""), {FULL_ID: (info, "")}):
            state = collect_garage_state(cfg)

        assert state is not None
        assert len(state.buckets) == 1
        assert state.buckets[0].id == FULL_ID
        assert state.buckets[0].alias == ""

    def test_multi_bucket_all_collected(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        infos = {
            FULL_ID: (_admin_info(FULL_ID, bytes_=1, objects=1, global_aliases=("a",)), ""),
            FULL_ID_2: (_admin_info(FULL_ID_2, bytes_=2, objects=2, global_aliases=("b",)), ""),
        }
        with _patched(_CLI_OK, ([{"id": FULL_ID}, {"id": FULL_ID_2}], ""), infos):
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
        with _patched(_CLI_OK, ([{"id": FULL_ID}, {"id": FULL_ID_2}], ""), infos):
            state = collect_garage_state(cfg)

        assert state is not None
        assert [b.alias for b in state.buckets] == ["ok"]

    def test_list_buckets_failure_skips_tick(self, tmp_path: Path) -> None:
        """ListBuckets unreachable -> None (skip the whole push, don't report empty)."""
        cfg = _make_config(tmp_path)
        with _patched(_CLI_OK, (None, "Could not reach admin API"), {}):
            assert collect_garage_state(cfg) is None

    def test_admin_unconfigured_skips_tick(self, tmp_path: Path) -> None:
        """No admin_url/admin_token -> can't read buckets -> skip the tick."""
        cfg = _make_config(tmp_path, admin_url="")
        # admin_api is never reached; only the CLI telemetry is mocked.
        with patch(
            "stormpulse.garage.state.run_garage",
            side_effect=_mock_run_garage(_CLI_OK),
        ):
            assert collect_garage_state(cfg) is None

    def test_empty_cluster_no_buckets(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with _patched(_CLI_OK, ([], ""), {}):
            state = collect_garage_state(cfg)
        assert state is not None
        assert state.buckets == []

    def test_status_failure_returns_none(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with _patched({}, ([], ""), {}):
            assert collect_garage_state(cfg) is None

    def test_status_parse_error_returns_none(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with (
            _patched({("status",): STATUS_OUTPUT}, ([], ""), {}),
            patch(
                "stormpulse.garage.state.parse_status",
                side_effect=GarageParseError("bad"),
            ),
        ):
            assert collect_garage_state(cfg) is None

    def test_empty_status_returns_none(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with _patched({("status",): STATUS_OUTPUT_EMPTY}, ([], ""), {}):
            assert collect_garage_state(cfg) is None

    def test_stats_failure_still_returns_state(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        cli = {("status",): STATUS_OUTPUT, ("stats",): None, ("key", "list"): None}
        with _patched(cli, ([], ""), {}):
            state = collect_garage_state(cfg)
        assert state is not None
        assert state.db_engine == "unknown"
        assert state.buckets == []

    def test_stats_parse_error_falls_back_to_defaults(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with (
            _patched(_CLI_OK, ([], ""), {}),
            patch(
                "stormpulse.garage.state.parse_stats",
                side_effect=GarageParseError("bad"),
            ),
        ):
            state = collect_garage_state(cfg)
        assert state is not None
        assert state.db_engine == "unknown"
        assert state.object_count == 0
        assert state.block_count == 0

    def test_key_list_parse_error_empties_top_level_not_bucket_keys(
        self, tmp_path: Path,
    ) -> None:
        """A key-list parse error empties the top-level inventory, but bucket keys
        survive because GetBucketInfo carries their names inline now."""
        cfg = _make_config(tmp_path)
        info = _admin_info(
            FULL_ID,
            bytes_=1,
            objects=1,
            global_aliases=("vault",),
            keys=(_admin_key("GK5e6fb0b4fa406ace8126a7db", "obsidian-key"),),
        )
        with (
            _patched(_CLI_OK, ([{"id": FULL_ID}], ""), {FULL_ID: (info, "")}),
            patch(
                "stormpulse.garage.state.parse_key_list",
                side_effect=GarageParseError("bad"),
            ),
        ):
            state = collect_garage_state(cfg)
        assert state is not None
        assert state.keys == []
        assert state.buckets[0].keys[0].key_name == "obsidian-key"

    def test_multi_node_all_peers_collected(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        cli = {
            ("status",): STATUS_OUTPUT_MULTI_NODE,
            ("stats",): STATS_OUTPUT,
            ("key", "list"): KEY_LIST_OUTPUT,
        }
        with _patched(cli, ([], ""), {}):
            state = collect_garage_state(cfg)
        assert state is not None
        assert len(state.peers) == 3
        assert {p.hostname for p in state.peers} == {
            "garage-one", "garage-two", "garage-pi",
        }
        assert state.node_id == "7a58a5fa192ad6dd"

    def test_unlinked_keys_appear_in_top_level(self, tmp_path: Path) -> None:
        """Keys in `key list` but not on any bucket still surface at top level."""
        cfg = _make_config(tmp_path)
        cli = {**_CLI_OK, ("key", "list"): KEY_LIST_OUTPUT_MULTI}
        info = _admin_info(
            FULL_ID,
            bytes_=1,
            objects=1,
            global_aliases=("vault",),
            keys=(_admin_key("GK5e6fb0b4fa406ace8126a7db", "obsidian-key"),),
        )
        with _patched(cli, ([{"id": FULL_ID}], ""), {FULL_ID: (info, "")}):
            state = collect_garage_state(cfg)
        assert state is not None
        assert len(state.buckets[0].keys) == 1
        assert {k.key_name for k in state.keys} == {"obsidian-key", "backup-key"}

    def test_to_dict(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        info = _admin_info(
            FULL_ID, bytes_=5800, objects=2, global_aliases=("obsidian-vault",),
        )
        with _patched(_CLI_OK, ([{"id": FULL_ID}], ""), {FULL_ID: (info, "")}):
            state = collect_garage_state(cfg)

        assert state is not None
        d = state.to_dict()
        assert d["node_id"] == "7a58a5fa192ad6dd"
        assert isinstance(d["buckets"], list)
        assert d["buckets"][0]["alias"] == "obsidian-vault"
        assert d["buckets"][0]["size_bytes"] == 5800
        assert isinstance(d["peers"], list)
        assert len(d["peers"]) == 1
        assert d["peers"][0]["node_id"] == "7a58a5fa192ad6dd"


class TestRunGarage:
    """Direct tests for run_garage subprocess failure modes."""

    def test_returns_stdout_on_success(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="hello\n", stderr="",
        )
        with patch("stormpulse.garage.state.subprocess.run", return_value=completed):
            assert run_garage(cfg, "status") == "hello\n"

    def test_file_not_found_returns_none(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with patch(
            "stormpulse.garage.state.subprocess.run",
            side_effect=FileNotFoundError(),
        ):
            assert run_garage(cfg, "status") is None

    def test_timeout_returns_none(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with patch(
            "stormpulse.garage.state.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="garage", timeout=15),
        ):
            assert run_garage(cfg, "status") is None

    def test_nonzero_exit_returns_none(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="boom\n",
        )
        with patch("stormpulse.garage.state.subprocess.run", return_value=completed):
            assert run_garage(cfg, "status") is None
