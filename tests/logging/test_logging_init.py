"""Tests for stormpulse.logging.init - the registered init step."""

from __future__ import annotations

import tomllib
from pathlib import Path
from unittest.mock import patch

from stormpulse.logging.init import logging_init_step

_BASE_TOML = '[agent]\nid = "x"\n'


class TestLoggingInitStep:
    def test_no_containers_skips(self, tmp_path: Path) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text(_BASE_TOML)
        with patch(
            "stormpulse.logging.init.detect_docker_containers",
            return_value=[],
        ):
            logging_init_step(cfg)
        assert "log_groups" not in cfg.read_text()

    def test_appends_log_groups(self, tmp_path: Path) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text(_BASE_TOML)
        groups: list[dict[str, str | int]] = [{
            "name": "web",
            "container_name": "web",
            "docker_binary": "/usr/bin/docker",
            "ship_interval_seconds": 10,
            "parser": "docker_raw",
            "filter_contains": "",
        }]
        with patch(
            "stormpulse.logging.init.detect_docker_containers",
            return_value=["web"],
        ), patch(
            "stormpulse.logging.init.prompt_logging_setup",
            return_value=groups,
        ):
            logging_init_step(cfg)
        with open(cfg, "rb") as f:
            raw = tomllib.load(f)
        assert raw["log_groups"][0]["name"] == "web"
