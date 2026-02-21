"""Tests for stormpulse.config."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from stormpulse.config import ConfigError, load_config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

EXAMPLE_CONFIG = Path(__file__).parent.parent / "config" / "stormpulse.example.toml"

MINIMAL_VALID = """\
[agent]
id = "test-01"

[dashboard]
url = "wss://example.com/ws/"
reconnect_min_seconds = 1
reconnect_max_seconds = 30

[tls]
ca_cert = "/tmp/ca.pem"
client_cert = "/tmp/agent.pem"
client_key = "/tmp/agent-key.pem"

[auth]
hmac_secret = "/tmp/hmac.key"
command_max_age_seconds = 60

[metrics]
push_interval_seconds = 10
collect_containers = false

[project]
project_dir = "/tmp/project"
compose_file = "/tmp/project/docker-compose.yml"
docker_service_name = "web"

[storage]
db_path = "/tmp/stormpulse.db"
"""


@pytest.fixture
def write_config(tmp_path: Path) -> Callable[[str], Path]:
    """Write a TOML string to a temp file and return its path."""
    def _write(content: str) -> Path:
        p = tmp_path / "stormpulse.toml"
        p.write_text(content)
        return p
    return _write


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_load_example_config() -> None:
    config = load_config(EXAMPLE_CONFIG)
    assert config.agent.id == "vps-toronto-01"
    assert config.dashboard.url == "wss://stormdevelopments.ca/ws/pulse/"
    assert config.dashboard.reconnect_min_seconds == 3.0
    assert config.dashboard.reconnect_max_seconds == 60.0
    assert config.metrics.collect_containers is True
    assert config.metrics.push_interval_seconds == 15.0
    assert config.auth.command_max_age_seconds == 60
    assert isinstance(config.tls.ca_cert, Path)
    assert isinstance(config.storage.db_path, Path)


def test_load_minimal_valid(write_config: Callable[[str], Path]) -> None:
    config = load_config(write_config(MINIMAL_VALID))
    assert config.agent.id == "test-01"
    assert config.dashboard.reconnect_min_seconds == 1.0
    assert config.metrics.collect_containers is False


def test_config_types(write_config: Callable[[str], Path]) -> None:
    config = load_config(write_config(MINIMAL_VALID))
    assert isinstance(config.dashboard.reconnect_min_seconds, float)
    assert isinstance(config.auth.command_max_age_seconds, int)
    assert isinstance(config.metrics.collect_containers, bool)
    assert isinstance(config.project.project_dir, Path)
    assert isinstance(config.project.compose_file, Path)


def test_int_coerced_to_float(write_config: Callable[[str], Path]) -> None:
    """TOML integer values for float fields should work."""
    config = load_config(write_config(MINIMAL_VALID))
    assert config.dashboard.reconnect_min_seconds == 1.0
    assert type(config.dashboard.reconnect_min_seconds) is float


# ---------------------------------------------------------------------------
# Missing sections
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("section", [
    "agent", "dashboard", "tls", "auth", "metrics", "project", "storage",
])
def test_missing_section_raises(write_config: Callable[[str], Path], section: str) -> None:
    lines = [line for line in MINIMAL_VALID.splitlines(keepends=True)
             if not line.strip().startswith(f"[{section}]")]
    filtered: list[str] = []
    skip = False
    for line in lines:
        if line.strip().startswith("[") and not line.strip().startswith(f"[{section}]"):
            skip = False
        if line.strip() == f"[{section}]":
            skip = True
            continue
        if not skip:
            filtered.append(line)
    content = "".join(filtered)
    with pytest.raises(ConfigError, match=section):
        load_config(write_config(content))


# ---------------------------------------------------------------------------
# Missing keys
# ---------------------------------------------------------------------------


def test_missing_agent_id_raises(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace('id = "test-01"', "")
    with pytest.raises(ConfigError, match="id"):
        load_config(write_config(content))


def test_missing_dashboard_url_raises(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace('url = "wss://example.com/ws/"', "")
    with pytest.raises(ConfigError, match="url"):
        load_config(write_config(content))


def test_missing_hmac_secret_raises(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace('hmac_secret = "/tmp/hmac.key"', "")
    with pytest.raises(ConfigError, match="hmac_secret"):
        load_config(write_config(content))


# ---------------------------------------------------------------------------
# Type errors
# ---------------------------------------------------------------------------


def test_wrong_type_for_url_raises(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace('url = "wss://example.com/ws/"', "url = 42")
    with pytest.raises(ConfigError, match="str"):
        load_config(write_config(content))


def test_wrong_type_for_collect_containers_raises(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace("collect_containers = false", 'collect_containers = "yes"')
    with pytest.raises(ConfigError, match="bool"):
        load_config(write_config(content))


# ---------------------------------------------------------------------------
# Range validation
# ---------------------------------------------------------------------------


def test_negative_reconnect_raises(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace("reconnect_min_seconds = 1", "reconnect_min_seconds = -1")
    with pytest.raises(ConfigError, match="positive"):
        load_config(write_config(content))


def test_zero_reconnect_raises(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace("reconnect_min_seconds = 1", "reconnect_min_seconds = 0")
    with pytest.raises(ConfigError, match="positive"):
        load_config(write_config(content))


def test_min_greater_than_max_raises(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace("reconnect_min_seconds = 1", "reconnect_min_seconds = 100")
    with pytest.raises(ConfigError, match="<="):
        load_config(write_config(content))


def test_negative_max_age_raises(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace("command_max_age_seconds = 60", "command_max_age_seconds = -1")
    with pytest.raises(ConfigError, match="positive"):
        load_config(write_config(content))


def test_zero_max_age_raises(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace("command_max_age_seconds = 60", "command_max_age_seconds = 0")
    with pytest.raises(ConfigError, match="positive"):
        load_config(write_config(content))


def test_negative_push_interval_raises(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace("push_interval_seconds = 10", "push_interval_seconds = 0")
    with pytest.raises(ConfigError, match="positive"):
        load_config(write_config(content))


# ---------------------------------------------------------------------------
# File handling
# ---------------------------------------------------------------------------


def test_nonexistent_file_raises() -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(Path("/nonexistent/stormpulse.toml"))


def test_invalid_toml_raises(write_config: Callable[[str], Path]) -> None:
    with pytest.raises(ConfigError, match="Invalid TOML"):
        load_config(write_config("this is [not valid toml"))


def test_section_not_a_table_raises(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace('[agent]\nid = "test-01"', 'agent = "not a table"')
    with pytest.raises(ConfigError, match="table"):
        load_config(write_config(content))


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


def test_validate_paths_missing_cert() -> None:
    config = load_config(EXAMPLE_CONFIG)
    with pytest.raises(ConfigError, match="Missing files"):
        config.validate_paths()


def test_config_is_frozen(write_config: Callable[[str], Path]) -> None:
    config = load_config(write_config(MINIMAL_VALID))
    with pytest.raises(AttributeError):
        config.agent = None  # type: ignore[misc,assignment]
