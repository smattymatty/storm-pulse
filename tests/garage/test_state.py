"""Tests for stormpulse.garage.state - state collection with mocked subprocess."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from stormpulse.config import GarageConfig
from stormpulse.garage.parse import GarageParseError
from stormpulse.garage.state import collect_garage_state, run_garage
from tests.garage.fixtures import (
    BUCKET_INFO_OUTPUT,
    BUCKET_INFO_OUTPUT_NO_GLOBAL_ALIAS,
    BUCKET_INFO_OUTPUT_WITH_QUOTAS,
    BUCKET_LIST_OUTPUT,
    BUCKET_LIST_OUTPUT_EMPTY,
    BUCKET_LIST_OUTPUT_MULTI,
    KEY_LIST_OUTPUT,
    KEY_LIST_OUTPUT_MULTI,
    STATS_OUTPUT,
    STATUS_OUTPUT,
    STATUS_OUTPUT_EMPTY,
    STATUS_OUTPUT_MULTI_NODE,
)


def _make_config(tmp_path: Path) -> GarageConfig:
    return GarageConfig(
        enabled=True,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=tmp_path / "garage.toml",
        state_push_interval_seconds=300,
    )


def _mock_run_garage(outputs: dict[tuple[str, ...], str | None]) -> object:
    """Create a side_effect function for run_garage that maps args to outputs."""

    def side_effect(config: GarageConfig, *args: str) -> str | None:
        return outputs.get(args)

    return side_effect


class TestCollectGarageState:
    def test_full_collection(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        outputs: dict[tuple[str, ...], str | None] = {
            ("status",): STATUS_OUTPUT,
            ("stats",): STATS_OUTPUT,
            ("key", "list"): KEY_LIST_OUTPUT,
            ("bucket", "list"): BUCKET_LIST_OUTPUT,
            ("bucket", "info", "obsidian-vault"): BUCKET_INFO_OUTPUT,
        }
        with patch(
            "stormpulse.garage.state.run_garage",
            side_effect=_mock_run_garage(outputs),
        ):
            state = collect_garage_state(cfg)

        assert state is not None
        assert state.node_id == "7a58a5fa192ad6dd"
        assert state.hostname == "garage-one"
        assert state.zone == "canada-1"
        assert state.healthy is True
        assert state.db_engine == "sqlite3 v3.50.2 (using rusqlite crate)"
        assert state.object_count == 5
        assert state.block_count == 1
        assert len(state.buckets) == 1
        bucket = state.buckets[0]
        assert bucket.alias == "obsidian-vault"
        assert bucket.size_bytes == 5800
        assert bucket.object_count == 2
        assert len(bucket.keys) == 1
        assert bucket.keys[0].key_id == "GK5e6fb0b4fa406ace8126a7db"
        # key_name resolved from key list, not from bucket info local_alias
        assert bucket.keys[0].key_name == "obsidian-key"
        assert bucket.keys[0].permissions == "RWO"
        assert bucket.website_access is False
        assert bucket.website_index_document == "index.html"
        assert bucket.website_error_document is None
        assert bucket.quota_max_size_bytes is None
        assert bucket.quota_max_objects is None
        # Top-level keys list includes all keys (even unlinked)
        assert len(state.keys) == 1
        assert state.keys[0].key_id == "GK5e6fb0b4fa406ace8126a7db"
        assert state.keys[0].key_name == "obsidian-key"
        assert state.keys[0].permissions == ""
        # Peers list includes all nodes from garage status
        assert len(state.peers) == 1
        assert state.peers[0].node_id == "7a58a5fa192ad6dd"
        assert state.peers[0].hostname == "garage-one"

    def test_bucket_with_quotas(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        outputs: dict[tuple[str, ...], str | None] = {
            ("status",): STATUS_OUTPUT,
            ("stats",): STATS_OUTPUT,
            ("key", "list"): KEY_LIST_OUTPUT,
            ("bucket", "list"): BUCKET_LIST_OUTPUT,
            ("bucket", "info", "obsidian-vault"): BUCKET_INFO_OUTPUT_WITH_QUOTAS,
        }
        with patch(
            "stormpulse.garage.state.run_garage",
            side_effect=_mock_run_garage(outputs),
        ):
            state = collect_garage_state(cfg)

        assert state is not None
        bucket = state.buckets[0]
        assert bucket.website_access is False
        assert bucket.quota_max_size_bytes == 1_000_000_000
        assert bucket.quota_max_objects == 1000

    def test_status_failure_returns_none(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with patch(
            "stormpulse.garage.state.run_garage",
            return_value=None,
        ):
            assert collect_garage_state(cfg) is None

    def test_stats_failure_still_returns_state(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        outputs = {
            ("status",): STATUS_OUTPUT,
            ("stats",): None,
            ("key", "list"): None,
            ("bucket", "list"): None,
        }
        with patch(
            "stormpulse.garage.state.run_garage",
            side_effect=_mock_run_garage(outputs),
        ):
            state = collect_garage_state(cfg)

        assert state is not None
        assert state.db_engine == "unknown"
        assert state.buckets == []

    def test_status_parse_error_returns_none(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        outputs: dict[tuple[str, ...], str | None] = {("status",): STATUS_OUTPUT}
        with (
            patch(
                "stormpulse.garage.state.run_garage",
                side_effect=_mock_run_garage(outputs),
            ),
            patch(
                "stormpulse.garage.state.parse_status",
                side_effect=GarageParseError("bad"),
            ),
        ):
            assert collect_garage_state(cfg) is None

    def test_stats_parse_error_falls_back_to_defaults(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        outputs: dict[tuple[str, ...], str | None] = {
            ("status",): STATUS_OUTPUT,
            ("stats",): STATS_OUTPUT,
            ("key", "list"): KEY_LIST_OUTPUT,
            ("bucket", "list"): BUCKET_LIST_OUTPUT_EMPTY,
        }
        with (
            patch(
                "stormpulse.garage.state.run_garage",
                side_effect=_mock_run_garage(outputs),
            ),
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

    def test_key_list_parse_error_gives_empty_map(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        outputs: dict[tuple[str, ...], str | None] = {
            ("status",): STATUS_OUTPUT,
            ("stats",): STATS_OUTPUT,
            ("key", "list"): KEY_LIST_OUTPUT,
            ("bucket", "list"): BUCKET_LIST_OUTPUT,
            ("bucket", "info", "obsidian-vault"): BUCKET_INFO_OUTPUT,
        }
        with (
            patch(
                "stormpulse.garage.state.run_garage",
                side_effect=_mock_run_garage(outputs),
            ),
            patch(
                "stormpulse.garage.state.parse_key_list",
                side_effect=GarageParseError("bad"),
            ),
        ):
            state = collect_garage_state(cfg)
        assert state is not None
        assert state.keys == []
        # Bucket still present, but key_name empty since map is empty
        assert state.buckets[0].keys[0].key_name == ""

    def test_bucket_list_parse_error_gives_no_buckets(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        outputs: dict[tuple[str, ...], str | None] = {
            ("status",): STATUS_OUTPUT,
            ("stats",): STATS_OUTPUT,
            ("key", "list"): KEY_LIST_OUTPUT,
            ("bucket", "list"): BUCKET_LIST_OUTPUT,
        }
        with (
            patch(
                "stormpulse.garage.state.run_garage",
                side_effect=_mock_run_garage(outputs),
            ),
            patch(
                "stormpulse.garage.state.parse_bucket_list",
                side_effect=GarageParseError("bad"),
            ),
        ):
            state = collect_garage_state(cfg)
        assert state is not None
        assert state.buckets == []

    def test_bucket_info_parse_error_skips_one_bucket(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        outputs: dict[tuple[str, ...], str | None] = {
            ("status",): STATUS_OUTPUT,
            ("stats",): STATS_OUTPUT,
            ("key", "list"): KEY_LIST_OUTPUT_MULTI,
            ("bucket", "list"): BUCKET_LIST_OUTPUT_MULTI,
            ("bucket", "info", "obsidian-vault"): BUCKET_INFO_OUTPUT,
            ("bucket", "info", "backups"): "GARBAGE",
        }
        real_parse_bucket_info = __import__(
            "stormpulse.garage.parse",
            fromlist=["parse_bucket_info"],
        ).parse_bucket_info

        def flaky_parse(out: str) -> object:
            if out == "GARBAGE":
                raise GarageParseError("bad")
            return real_parse_bucket_info(out)

        with (
            patch(
                "stormpulse.garage.state.run_garage",
                side_effect=_mock_run_garage(outputs),
            ),
            patch(
                "stormpulse.garage.state.parse_bucket_info",
                side_effect=flaky_parse,
            ),
        ):
            state = collect_garage_state(cfg)
        assert state is not None
        assert [b.alias for b in state.buckets] == ["obsidian-vault"]

    def test_empty_status_returns_none(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        outputs: dict[tuple[str, ...], str | None] = {
            ("status",): STATUS_OUTPUT_EMPTY,
        }
        with patch(
            "stormpulse.garage.state.run_garage",
            side_effect=_mock_run_garage(outputs),
        ):
            assert collect_garage_state(cfg) is None

    def test_multi_node_all_peers_collected(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        outputs: dict[tuple[str, ...], str | None] = {
            ("status",): STATUS_OUTPUT_MULTI_NODE,
            ("stats",): STATS_OUTPUT,
            ("key", "list"): KEY_LIST_OUTPUT,
            ("bucket", "list"): BUCKET_LIST_OUTPUT_EMPTY,
        }
        with patch(
            "stormpulse.garage.state.run_garage",
            side_effect=_mock_run_garage(outputs),
        ):
            state = collect_garage_state(cfg)
        assert state is not None
        assert len(state.peers) == 3
        assert {p.hostname for p in state.peers} == {
            "garage-one",
            "garage-two",
            "garage-pi",
        }
        # node_id picks first node
        assert state.node_id == "7a58a5fa192ad6dd"

    def test_multi_bucket_all_collected(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        outputs: dict[tuple[str, ...], str | None] = {
            ("status",): STATUS_OUTPUT,
            ("stats",): STATS_OUTPUT,
            ("key", "list"): KEY_LIST_OUTPUT_MULTI,
            ("bucket", "list"): BUCKET_LIST_OUTPUT_MULTI,
            ("bucket", "info", "obsidian-vault"): BUCKET_INFO_OUTPUT,
            ("bucket", "info", "backups"): BUCKET_INFO_OUTPUT_WITH_QUOTAS,
        }
        with patch(
            "stormpulse.garage.state.run_garage",
            side_effect=_mock_run_garage(outputs),
        ):
            state = collect_garage_state(cfg)
        assert state is not None
        assert {b.alias for b in state.buckets} == {"obsidian-vault", "backups"}

    def test_bucket_without_global_alias_addressed_by_uuid(
        self, tmp_path: Path
    ) -> None:
        """Alias-less buckets (post-bucket-naming-refactor) must still be collected.

        Most customer buckets won't have a global alias - only website-hosted ones
        do. The agent addresses them by UUID (Garage CLI accepts a UUID anywhere
        it accepts a global alias). The metrics push entry carries the UUID in
        ``id`` (Cellar joins on this) and an empty ``alias``.
        """
        cfg = _make_config(tmp_path)
        from stormpulse.garage.parse import GarageBucketListEntry

        bucket_uuid = "a9b8c7d6e5f40321"
        outputs: dict[tuple[str, ...], str | None] = {
            ("status",): STATUS_OUTPUT,
            ("stats",): STATS_OUTPUT,
            ("key", "list"): KEY_LIST_OUTPUT,
            ("bucket", "list"): BUCKET_LIST_OUTPUT,
            # Bucket info is addressed by UUID, not alias.
            ("bucket", "info", bucket_uuid): BUCKET_INFO_OUTPUT_NO_GLOBAL_ALIAS,
        }
        with (
            patch(
                "stormpulse.garage.state.run_garage",
                side_effect=_mock_run_garage(outputs),
            ),
            patch(
                "stormpulse.garage.state.parse_bucket_list",
                return_value=[
                    GarageBucketListEntry(bucket_id=bucket_uuid, global_alias="")
                ],
            ),
        ):
            state = collect_garage_state(cfg)
        assert state is not None
        assert len(state.buckets) == 1
        bucket = state.buckets[0]
        # id is the full UUID parsed from bucket info (how Cellar joins).
        assert (
            bucket.id
            == "a9b8c7d6e5f4032110aabbccddeeff00112233445566778899aabbccddeeff00"
        )
        assert bucket.alias == ""
        assert bucket.size_bytes == 5800
        assert bucket.object_count == 2

    def test_bucket_info_addressed_by_uuid_when_no_alias(self, tmp_path: Path) -> None:
        """Verify the subprocess call uses the UUID, not an alias, for alias-less buckets."""
        cfg = _make_config(tmp_path)
        from stormpulse.garage.parse import GarageBucketListEntry

        bucket_uuid = "a9b8c7d6e5f40321"
        called_args: list[tuple[str, ...]] = []

        def recording_run_garage(config: GarageConfig, *args: str) -> str | None:
            called_args.append(args)
            return {
                ("status",): STATUS_OUTPUT,
                ("stats",): STATS_OUTPUT,
                ("key", "list"): KEY_LIST_OUTPUT,
                ("bucket", "list"): BUCKET_LIST_OUTPUT,
                ("bucket", "info", bucket_uuid): BUCKET_INFO_OUTPUT_NO_GLOBAL_ALIAS,
            }.get(args)

        with (
            patch(
                "stormpulse.garage.state.run_garage",
                side_effect=recording_run_garage,
            ),
            patch(
                "stormpulse.garage.state.parse_bucket_list",
                return_value=[
                    GarageBucketListEntry(bucket_id=bucket_uuid, global_alias="")
                ],
            ),
        ):
            collect_garage_state(cfg)

        assert ("bucket", "info", bucket_uuid) in called_args

    def test_unlinked_keys_appear_in_top_level(self, tmp_path: Path) -> None:
        """Keys in `key list` but not attached to any bucket still surface for the dashboard."""
        cfg = _make_config(tmp_path)
        outputs: dict[tuple[str, ...], str | None] = {
            ("status",): STATUS_OUTPUT,
            ("stats",): STATS_OUTPUT,
            ("key", "list"): KEY_LIST_OUTPUT_MULTI,  # 2 keys
            ("bucket", "list"): BUCKET_LIST_OUTPUT,  # 1 bucket
            ("bucket", "info", "obsidian-vault"): BUCKET_INFO_OUTPUT,  # uses 1 key
        }
        with patch(
            "stormpulse.garage.state.run_garage",
            side_effect=_mock_run_garage(outputs),
        ):
            state = collect_garage_state(cfg)
        assert state is not None
        # Bucket has 1 attached key, but top-level has both (including unlinked "backup-key")
        assert len(state.buckets[0].keys) == 1
        assert {k.key_name for k in state.keys} == {"obsidian-key", "backup-key"}

    def test_to_dict(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        outputs: dict[tuple[str, ...], str | None] = {
            ("status",): STATUS_OUTPUT,
            ("stats",): STATS_OUTPUT,
            ("key", "list"): KEY_LIST_OUTPUT,
            ("bucket", "list"): BUCKET_LIST_OUTPUT,
            ("bucket", "info", "obsidian-vault"): BUCKET_INFO_OUTPUT,
        }
        with patch(
            "stormpulse.garage.state.run_garage",
            side_effect=_mock_run_garage(outputs),
        ):
            state = collect_garage_state(cfg)

        assert state is not None
        d = state.to_dict()
        assert d["node_id"] == "7a58a5fa192ad6dd"
        assert isinstance(d["buckets"], list)
        assert d["buckets"][0]["alias"] == "obsidian-vault"
        assert isinstance(d["peers"], list)
        assert len(d["peers"]) == 1
        assert d["peers"][0]["node_id"] == "7a58a5fa192ad6dd"


class TestRunGarage:
    """Direct tests for run_garage subprocess failure modes."""

    def test_returns_stdout_on_success(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="hello\n",
            stderr="",
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
            args=[],
            returncode=1,
            stdout="",
            stderr="boom\n",
        )
        with patch("stormpulse.garage.state.subprocess.run", return_value=completed):
            assert run_garage(cfg, "status") is None
