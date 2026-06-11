"""Tests for stormpulse.garage.discover."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from stormpulse.config import GarageConfig
from stormpulse.garage.discover import discover_garage
from stormpulse.garage.state import GarageState


def _make_config(tmp_path: Path, *, enabled: bool = True) -> GarageConfig:
    config_path = tmp_path / "garage.toml"
    config_path.write_text("[s3_api]\n")
    return GarageConfig(
        enabled=enabled,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=config_path,
        state_push_interval_seconds=300,
    )


class TestDiscoverGarage:
    def test_none_config(self) -> None:
        assert discover_garage(None) is None

    def test_disabled(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path, enabled=False)
        assert discover_garage(cfg) is None

    def test_missing_config_path(self, tmp_path: Path) -> None:
        cfg = GarageConfig(
            enabled=True,
            container_name="garaged",
            garage_binary="/garage",
            docker_binary="/usr/bin/docker",
            config_path=tmp_path / "nonexistent.toml",
            state_push_interval_seconds=300,
        )
        assert discover_garage(cfg) is None

    def test_successful_discovery(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        fake_state = GarageState(
            node_id="7a58a5fa192ad6dd",
            hostname="garage-one",
            zone="canada-1",
            capacity_gb=10.0,
            data_avail_gb=16.3,
            version="v2.2.0",
            healthy=True,
            object_count=2,
            buckets=[],
            keys=[],
            peers=[],
        )
        with patch(
            "stormpulse.garage.discover.collect_garage_state",
            return_value=fake_state,
        ):
            result = discover_garage(cfg)
        assert result is not None
        assert result.node_id == "7a58a5fa192ad6dd"

    def test_collect_returns_none(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        with patch(
            "stormpulse.garage.discover.collect_garage_state",
            return_value=None,
        ):
            assert discover_garage(cfg) is None
