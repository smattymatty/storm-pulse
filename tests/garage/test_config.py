"""Tests for GarageConfig parsing (CORE-005: parser lives in garage/config.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from stormpulse.config import ConfigError, load_config
from stormpulse.garage.config import parse_garage_config


def _write_toml(tmp_path: Path, garage_section: str = "") -> Path:
    """Write a minimal valid TOML config with optional [garage] section."""
    config = tmp_path / "stormpulse.toml"
    config.write_text(f"""\
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

{garage_section}
""")
    return config


def _garage_section(path: Path) -> dict[str, object]:
    """Load config and return the raw [garage] table the bootstrap loop parses."""
    return load_config(path).integrations["garage"]


class TestGarageConfigParsing:
    def test_absent_section_not_in_integrations(self, tmp_path: Path) -> None:
        path = _write_toml(tmp_path)
        cfg = load_config(path)
        assert "garage" not in cfg.integrations

    def test_valid_section(self, tmp_path: Path) -> None:
        path = _write_toml(
            tmp_path,
            """\
[garage]
enabled = true
container_name = "garaged"
garage_binary = "/garage"
docker_binary = "/usr/bin/docker"
config_path = "/opt/garage/garage.toml"
state_push_interval_seconds = 300
""",
        )
        gc = parse_garage_config(_garage_section(path))
        assert gc.enabled is True
        assert gc.container_name == "garaged"
        assert gc.garage_binary == "/garage"
        assert gc.docker_binary == "/usr/bin/docker"
        assert str(gc.config_path) == "/opt/garage/garage.toml"
        assert gc.state_push_interval_seconds == 300.0

    def test_disabled(self, tmp_path: Path) -> None:
        path = _write_toml(
            tmp_path,
            """\
[garage]
enabled = false
container_name = "garaged"
garage_binary = "/garage"
docker_binary = "/usr/bin/docker"
config_path = "/opt/garage/garage.toml"
state_push_interval_seconds = 300
""",
        )
        gc = parse_garage_config(_garage_section(path))
        assert gc.enabled is False

    def test_missing_required_key(self, tmp_path: Path) -> None:
        path = _write_toml(
            tmp_path,
            """\
[garage]
enabled = true
container_name = "garaged"
""",
        )
        with pytest.raises(ConfigError, match="garage_binary"):
            parse_garage_config(_garage_section(path))

    def test_non_absolute_docker_binary(self, tmp_path: Path) -> None:
        path = _write_toml(
            tmp_path,
            """\
[garage]
enabled = true
container_name = "garaged"
garage_binary = "/garage"
docker_binary = "docker"
config_path = "/opt/garage/garage.toml"
state_push_interval_seconds = 300
""",
        )
        with pytest.raises(ConfigError, match="absolute path"):
            parse_garage_config(_garage_section(path))

    def test_negative_interval(self, tmp_path: Path) -> None:
        path = _write_toml(
            tmp_path,
            """\
[garage]
enabled = true
container_name = "garaged"
garage_binary = "/garage"
docker_binary = "/usr/bin/docker"
config_path = "/opt/garage/garage.toml"
state_push_interval_seconds = -1
""",
        )
        with pytest.raises(ConfigError, match="positive"):
            parse_garage_config(_garage_section(path))

    def test_empty_container_name(self, tmp_path: Path) -> None:
        path = _write_toml(
            tmp_path,
            """\
[garage]
enabled = true
container_name = ""
garage_binary = "/garage"
docker_binary = "/usr/bin/docker"
config_path = "/opt/garage/garage.toml"
state_push_interval_seconds = 300
""",
        )
        with pytest.raises(ConfigError, match="container_name"):
            parse_garage_config(_garage_section(path))


class TestSensitiveOutputParsing:
    def test_sensitive_output_in_custom_command(self, tmp_path: Path) -> None:
        path = _write_toml(
            tmp_path,
            """\
[commands.my_cmd]
group = "test"
command = ["/usr/bin/echo", "hello"]
timeout = 10
sensitive_output = true
""",
        )
        cfg = load_config(path)
        assert cfg.commands["my_cmd"].sensitive_output is True

    def test_sensitive_output_defaults_false(self, tmp_path: Path) -> None:
        path = _write_toml(
            tmp_path,
            """\
[commands.my_cmd]
group = "test"
command = ["/usr/bin/echo", "hello"]
timeout = 10
""",
        )
        cfg = load_config(path)
        assert cfg.commands["my_cmd"].sensitive_output is False

    def test_sensitive_output_invalid_type(self, tmp_path: Path) -> None:
        path = _write_toml(
            tmp_path,
            """\
[commands.my_cmd]
group = "test"
command = ["/usr/bin/echo", "hello"]
timeout = 10
sensitive_output = "yes"
""",
        )
        with pytest.raises(ConfigError, match="sensitive_output"):
            load_config(path)
