"""Tests for stormpulse.garage.init - detection, compose parsing, TOML writing."""

from __future__ import annotations

import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from stormpulse.init import InitError
from stormpulse.garage.init import (
    append_garage_section,
    find_garage_config,
    garage_init_step,
    has_garage_section,
    parse_garage_container_name,
    remove_garage_section,
    run_garage_init,
)


# ---------------------------------------------------------------------------
# Compose parsing
# ---------------------------------------------------------------------------


COMPOSE_WITH_CONTAINER_NAME = """\
services:
  garage:
    image: dxflrs/garage:v2.2.0
    container_name: garaged
    network_mode: host
    restart: always
    volumes:
      - ./garage.toml:/etc/garage.toml
      - /var/lib/garage/meta:/var/lib/garage/meta
      - /var/lib/garage/data:/var/lib/garage/data

  caddy:
    image: caddy:2
    container_name: caddy
    restart: always
"""

COMPOSE_WITHOUT_CONTAINER_NAME = """\
services:
  garage:
    image: dxflrs/garage:v2.2.0
    network_mode: host
    restart: always

  caddy:
    image: caddy:2
"""

COMPOSE_CUSTOM_CONTAINER_NAME = """\
services:
  storage:
    image: dxflrs/garage:v2.2.0
    container_name: my-garage-node
    network_mode: host
"""

COMPOSE_NO_GARAGE = """\
services:
  web:
    image: nginx:latest
    ports:
      - "80:80"
"""


class TestParseGarageContainerName:
    def test_extracts_container_name(self, tmp_path: Path) -> None:
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(COMPOSE_WITH_CONTAINER_NAME)
        assert parse_garage_container_name(compose) == "garaged"

    def test_custom_container_name(self, tmp_path: Path) -> None:
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(COMPOSE_CUSTOM_CONTAINER_NAME)
        assert parse_garage_container_name(compose) == "my-garage-node"

    def test_defaults_without_container_name(self, tmp_path: Path) -> None:
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(COMPOSE_WITHOUT_CONTAINER_NAME)
        assert parse_garage_container_name(compose) == "garaged"

    def test_defaults_when_no_garage_service(self, tmp_path: Path) -> None:
        compose = tmp_path / "docker-compose.yml"
        compose.write_text(COMPOSE_NO_GARAGE)
        assert parse_garage_container_name(compose) == "garaged"

    def test_defaults_when_file_missing(self, tmp_path: Path) -> None:
        compose = tmp_path / "nonexistent.yml"
        assert parse_garage_container_name(compose) == "garaged"


# ---------------------------------------------------------------------------
# Garage config detection
# ---------------------------------------------------------------------------


class TestFindGarageConfig:
    def test_override_path(self, tmp_path: Path) -> None:
        cfg = tmp_path / "garage.toml"
        cfg.write_text("[s3_api]\n")
        assert find_garage_config(str(cfg)) == cfg

    def test_override_missing(self, tmp_path: Path) -> None:
        assert find_garage_config(str(tmp_path / "nope.toml")) is None

    def test_scan_order(self, tmp_path: Path) -> None:
        with patch(
            "stormpulse.garage.init._GARAGE_CONFIG_SEARCH_PATHS",
            [tmp_path / "a.toml", tmp_path / "b.toml"],
        ):
            assert find_garage_config() is None
            (tmp_path / "b.toml").write_text("")
            assert find_garage_config() == tmp_path / "b.toml"
            (tmp_path / "a.toml").write_text("")
            assert find_garage_config() == tmp_path / "a.toml"

    def test_glob_pattern_finds_rootless_storm_layout(self, tmp_path: Path) -> None:
        # Storm rootless convention: config under /home/<user>/garage/etc/
        # The init wizard must auto-discover that layout without forcing
        # the operator to pass --garage-config.
        home_user = tmp_path / "storm" / "garage" / "etc"
        home_user.mkdir(parents=True)
        cfg = home_user / "garage.toml"
        cfg.write_text("[s3_api]\n")
        glob_pattern = str(tmp_path / "*" / "garage" / "etc" / "garage.toml")
        with patch(
            "stormpulse.garage.init._GARAGE_CONFIG_SEARCH_PATHS", [],
        ), patch(
            "stormpulse.garage.init._GARAGE_CONFIG_GLOB_PATTERNS",
            [glob_pattern],
        ):
            assert find_garage_config() == cfg

    def test_fixed_paths_win_over_glob(self, tmp_path: Path) -> None:
        # An existing /opt/garage install must keep winning when a
        # parallel rootless tree exists; the glob is a fallback, not a
        # replacement for the legacy default.
        legacy = tmp_path / "opt" / "garage"
        legacy.mkdir(parents=True)
        legacy_cfg = legacy / "garage.toml"
        legacy_cfg.write_text("[s3_api]\n# legacy\n")

        rootless = tmp_path / "storm" / "garage" / "etc"
        rootless.mkdir(parents=True)
        rootless_cfg = rootless / "garage.toml"
        rootless_cfg.write_text("[s3_api]\n# rootless\n")

        with patch(
            "stormpulse.garage.init._GARAGE_CONFIG_SEARCH_PATHS",
            [legacy_cfg],
        ), patch(
            "stormpulse.garage.init._GARAGE_CONFIG_GLOB_PATTERNS",
            [str(tmp_path / "*" / "garage" / "etc" / "garage.toml")],
        ):
            assert find_garage_config() == legacy_cfg

    def test_glob_no_matches_returns_none(self, tmp_path: Path) -> None:
        glob_pattern = str(tmp_path / "*" / "garage" / "etc" / "garage.toml")
        with patch(
            "stormpulse.garage.init._GARAGE_CONFIG_SEARCH_PATHS", [],
        ), patch(
            "stormpulse.garage.init._GARAGE_CONFIG_GLOB_PATTERNS",
            [glob_pattern],
        ):
            assert find_garage_config() is None


# ---------------------------------------------------------------------------
# TOML section management
# ---------------------------------------------------------------------------


_BASE_TOML = """\
[agent]
id = "test-01"
pulse_token = "tok-123"

[dashboard]
url = "wss://example.com/ws/"
reconnect_min_seconds = 3
reconnect_max_seconds = 60
heartbeat_interval_seconds = 30

[tls]
ca_cert = "/tmp/ca.pem"
client_cert = "/tmp/agent.pem"
client_key = "/tmp/key.pem"

[auth]
hmac_secret = "/tmp/hmac.key"
command_max_age_seconds = 60

[metrics]
push_interval_seconds = 15
collect_containers = true

[project]
project_dir = "/opt/app"
compose_file = "/opt/app/docker-compose.yml"
docker_service_name = "web"

[storage]
db_path = "/var/lib/stormpulse/nonces.db"
"""

_GARAGE_BLOCK = """\

[garage]
enabled = true
container_name = "garaged"
garage_binary = "/garage"
docker_binary = "/usr/bin/docker"
config_path = "/opt/garage/garage.toml"
state_push_interval_seconds = 300
"""


class TestHasGarageSection:
    def test_no_section(self, tmp_path: Path) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text(_BASE_TOML)
        assert has_garage_section(cfg) is False

    def test_has_section(self, tmp_path: Path) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text(_BASE_TOML + _GARAGE_BLOCK)
        assert has_garage_section(cfg) is True

    def test_missing_file(self, tmp_path: Path) -> None:
        assert has_garage_section(tmp_path / "nope.toml") is False


class TestRemoveGarageSection:
    def test_removes_section(self) -> None:
        lines = (_BASE_TOML + _GARAGE_BLOCK).splitlines(keepends=True)
        result = remove_garage_section(lines)
        text = "".join(result)
        assert "[garage]" not in text
        assert "container_name" not in text
        # Other sections preserved
        assert "[agent]" in text
        assert "[storage]" in text

    def test_no_section_unchanged(self) -> None:
        lines = _BASE_TOML.splitlines(keepends=True)
        result = remove_garage_section(lines)
        assert "".join(result) == _BASE_TOML

    def test_section_in_middle(self) -> None:
        toml = (
            "[agent]\nid = \"x\"\n\n"
            "[garage]\nenabled = true\ncontainer_name = \"g\"\n\n"
            "[storage]\ndb_path = \"/tmp/db\"\n"
        )
        lines = toml.splitlines(keepends=True)
        result = remove_garage_section(lines)
        text = "".join(result)
        assert "[garage]" not in text
        assert "[agent]" in text
        assert "[storage]" in text


class TestAppendGarageSection:
    def test_append_to_clean_config(self, tmp_path: Path) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text(_BASE_TOML)
        append_garage_section(
            cfg,
            container_name="garaged",
            garage_binary="/garage",
            docker_binary="/usr/bin/docker",
            garage_config_path="/opt/garage/garage.toml",
            state_push_interval_seconds=300,
        )
        # Verify the result is valid TOML
        with open(cfg, "rb") as f:
            raw = tomllib.load(f)
        assert raw["garage"]["enabled"] is True
        assert raw["garage"]["container_name"] == "garaged"
        assert raw["garage"]["state_push_interval_seconds"] == 300
        # Original sections intact
        assert raw["agent"]["id"] == "test-01"
        assert raw["storage"]["db_path"] == "/var/lib/stormpulse/nonces.db"

    def test_rejects_duplicate_without_force(self, tmp_path: Path) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text(_BASE_TOML + _GARAGE_BLOCK)
        with pytest.raises(InitError, match="already exists"):
            append_garage_section(
                cfg,
                container_name="garaged",
                garage_binary="/garage",
                docker_binary="/usr/bin/docker",
                garage_config_path="/opt/garage/garage.toml",
                state_push_interval_seconds=300,
            )

    def test_force_replaces_existing(self, tmp_path: Path) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text(_BASE_TOML + _GARAGE_BLOCK)
        append_garage_section(
            cfg,
            container_name="my-garage",
            garage_binary="/opt/garage/bin/garage",
            docker_binary="/usr/bin/docker",
            garage_config_path="/etc/garage.toml",
            state_push_interval_seconds=60,
            force=True,
        )
        with open(cfg, "rb") as f:
            raw = tomllib.load(f)
        assert raw["garage"]["container_name"] == "my-garage"
        assert raw["garage"]["state_push_interval_seconds"] == 60
        # No duplicate sections
        content = cfg.read_text()
        assert content.count("[garage]") == 1

    def test_missing_config_file(self, tmp_path: Path) -> None:
        with pytest.raises(InitError, match="not found"):
            append_garage_section(
                tmp_path / "nope.toml",
                container_name="garaged",
                garage_binary="/garage",
                docker_binary="/usr/bin/docker",
                garage_config_path="/opt/garage/garage.toml",
                state_push_interval_seconds=300,
            )

    def test_preserves_comments(self, tmp_path: Path) -> None:
        cfg = tmp_path / "stormpulse.toml"
        commented = _BASE_TOML + "# This is a comment\n"
        cfg.write_text(commented)
        append_garage_section(
            cfg,
            container_name="garaged",
            garage_binary="/garage",
            docker_binary="/usr/bin/docker",
            garage_config_path="/opt/garage/garage.toml",
            state_push_interval_seconds=300,
        )
        content = cfg.read_text()
        assert "# This is a comment" in content
        with open(cfg, "rb") as f:
            raw = tomllib.load(f)
        assert raw["garage"]["enabled"] is True


# ---------------------------------------------------------------------------
# Writability gate (replaces the old root-only check; see ADR core/003)
# ---------------------------------------------------------------------------


class TestRunGarageInit:
    def test_unwritable_config_dir_exits(self, tmp_path: Path) -> None:
        # Parent dir does not exist - os.access(parent, W_OK) is False.
        bad_path = tmp_path / "nonexistent" / "stormpulse.toml"
        with pytest.raises(InitError, match="Cannot write"):
            run_garage_init(bad_path)

    def test_no_garage_config_found(self, tmp_path: Path) -> None:
        with patch(
            "stormpulse.garage.init.find_garage_config",
            return_value=None,
        ):
            with pytest.raises(InitError, match="No Garage installation"):
                run_garage_init(tmp_path / "stormpulse.toml")


# ---------------------------------------------------------------------------
# Init step (registered with the orchestrator)
# ---------------------------------------------------------------------------


class TestGarageInitStep:
    def test_no_garage_skips(self, tmp_path: Path) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text(_BASE_TOML)
        with patch(
            "stormpulse.garage.init.find_garage_config",
            return_value=None,
        ):
            garage_init_step(cfg)
        assert has_garage_section(cfg) is False

    def test_declined_skips(self, tmp_path: Path) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text(_BASE_TOML)
        gcfg = tmp_path / "garage.toml"
        gcfg.write_text("[s3_api]\n")
        with patch(
            "stormpulse.garage.init.find_garage_config", return_value=gcfg
        ), patch(
            "stormpulse.garage.init.prompt_confirm", return_value=False
        ):
            garage_init_step(cfg)
        assert has_garage_section(cfg) is False

    def test_accepted_appends_section(self, tmp_path: Path) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text(_BASE_TOML)
        gcfg = tmp_path / "garage.toml"
        gcfg.write_text("[s3_api]\n")
        values: dict[str, str | int] = {
            "container_name": "garaged",
            "garage_binary": "/garage",
            "docker_binary": "/usr/bin/docker",
            "garage_config_path": str(gcfg),
            "state_push_interval_seconds": 30,
        }
        with patch(
            "stormpulse.garage.init.find_garage_config", return_value=gcfg
        ), patch(
            "stormpulse.garage.init.prompt_confirm", return_value=True
        ), patch(
            "stormpulse.garage.init.prompt_garage_values", return_value=values
        ):
            garage_init_step(cfg)
        with open(cfg, "rb") as f:
            raw = tomllib.load(f)
        assert raw["garage"]["enabled"] is True
        assert raw["garage"]["container_name"] == "garaged"
