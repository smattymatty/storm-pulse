"""Tests for stormpulse.garage.state — state collection with mocked subprocess."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from stormpulse.config import GarageConfig
from stormpulse.garage.state import collect_garage_state
from tests.garage.fixtures import (
    BUCKET_INFO_OUTPUT,
    BUCKET_LIST_OUTPUT,
    KEY_LIST_OUTPUT,
    STATS_OUTPUT,
    STATUS_OUTPUT,
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
    """Create a side_effect function for _run_garage that maps args to outputs."""
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
            "stormpulse.garage.state._run_garage",
            side_effect=_mock_run_garage(outputs),
        ):
            state = collect_garage_state(cfg)

        assert state is not None
        assert state.node_id == "7a58a5fa192ad6dd"
        assert state.hostname == "garage-one"
        assert state.zone == "canada-1"
        assert state.healthy is True
        assert state.db_engine == "sqlite"
        assert state.object_count == 2
        assert state.block_count == 3
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

    def test_status_failure_returns_none(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with patch(
            "stormpulse.garage.state._run_garage",
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
            "stormpulse.garage.state._run_garage",
            side_effect=_mock_run_garage(outputs),
        ):
            state = collect_garage_state(cfg)

        assert state is not None
        assert state.db_engine == "unknown"
        assert state.buckets == []

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
            "stormpulse.garage.state._run_garage",
            side_effect=_mock_run_garage(outputs),
        ):
            state = collect_garage_state(cfg)

        assert state is not None
        d = state.to_dict()
        assert d["node_id"] == "7a58a5fa192ad6dd"
        assert isinstance(d["buckets"], list)
        assert d["buckets"][0]["alias"] == "obsidian-vault"
