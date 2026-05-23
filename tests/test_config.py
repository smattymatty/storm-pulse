"""Tests for stormpulse.config."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from stormpulse.config import CommandDef, ConfigError, load_config


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

EXAMPLE_CONFIG = Path(__file__).parent.parent / "config" / "stormpulse.example.toml"

MINIMAL_VALID = """\
[agent]
id = "test-01"
pulse_token = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

[dashboard]
url = "wss://example.com/ws/"
reconnect_min_seconds = 1
reconnect_max_seconds = 30
heartbeat_interval_seconds = 30

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
    assert config.dashboard.heartbeat_interval_seconds == 30.0
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


def test_zero_heartbeat_interval_raises(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace("heartbeat_interval_seconds = 30", "heartbeat_interval_seconds = 0")
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


# ---------------------------------------------------------------------------
# Optional env_file
# ---------------------------------------------------------------------------


def test_env_file_parsed(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace(
        'docker_service_name = "web"',
        'docker_service_name = "web"\nenv_file = "/opt/myapp/.env"',
    )
    config = load_config(write_config(content))
    assert config.project.env_file == Path("/opt/myapp/.env")


def test_env_file_omitted_is_none(write_config: Callable[[str], Path]) -> None:
    config = load_config(write_config(MINIMAL_VALID))
    assert config.project.env_file is None


def test_env_file_wrong_type_raises(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace(
        'docker_service_name = "web"',
        'docker_service_name = "web"\nenv_file = 123',
    )
    with pytest.raises(ConfigError, match="env_file"):
        load_config(write_config(content))


# ---------------------------------------------------------------------------
# Custom commands
# ---------------------------------------------------------------------------

CUSTOM_COMMAND_TOML = """
[commands.restart_caddy]
group = "maintenance"
command = ["/usr/bin/systemctl", "restart", "caddy.service"]
timeout = 30
requires_confirmation = true
description = "Restart Caddy reverse proxy"
"""


def test_no_commands_section_gives_empty_dict(write_config: Callable[[str], Path]) -> None:
    config = load_config(write_config(MINIMAL_VALID))
    assert config.commands == {}


def test_custom_command_parsed(write_config: Callable[[str], Path]) -> None:
    config = load_config(write_config(MINIMAL_VALID + CUSTOM_COMMAND_TOML))
    assert "restart_caddy" in config.commands
    cmd = config.commands["restart_caddy"]
    assert isinstance(cmd, CommandDef)
    assert cmd.group == "maintenance"
    assert cmd.command == ["/usr/bin/systemctl", "restart", "caddy.service"]
    assert cmd.timeout == 30
    assert cmd.requires_confirmation is True
    assert cmd.description == "Restart Caddy reverse proxy"


def test_custom_command_defaults(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[commands.simple]
group = "test"
command = ["/bin/true"]
timeout = 5
"""
    config = load_config(write_config(toml))
    cmd = config.commands["simple"]
    assert cmd.requires_confirmation is False
    assert cmd.description == ""
    assert cmd.long_running is False


def test_custom_command_long_running(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[commands.bulk_op]
group = "maintenance"
command = ["/usr/bin/true"]
timeout = 600
long_running = true
"""
    config = load_config(write_config(toml))
    assert config.commands["bulk_op"].long_running is True


def test_custom_command_long_running_wrong_type_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[commands.bad]
group = "x"
command = ["/bin/true"]
timeout = 10
long_running = "yes"
"""
    with pytest.raises(ConfigError, match="long_running"):
        load_config(write_config(toml))


def test_multiple_custom_commands(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[commands.cmd_a]
group = "a"
command = ["/bin/true"]
timeout = 10

[commands.cmd_b]
group = "b"
command = ["/bin/false"]
timeout = 20
"""
    config = load_config(write_config(toml))
    assert len(config.commands) == 2
    assert "cmd_a" in config.commands
    assert "cmd_b" in config.commands


def test_command_missing_group_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[commands.bad]
command = ["/bin/true"]
timeout = 10
"""
    with pytest.raises(ConfigError, match="group"):
        load_config(write_config(toml))


def test_command_missing_command_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[commands.bad]
group = "test"
timeout = 10
"""
    with pytest.raises(ConfigError, match="command"):
        load_config(write_config(toml))


def test_command_missing_timeout_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[commands.bad]
group = "test"
command = ["/bin/true"]
"""
    with pytest.raises(ConfigError, match="timeout"):
        load_config(write_config(toml))


def test_command_non_absolute_path_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[commands.bad]
group = "test"
command = ["relative/bin", "arg"]
timeout = 10
"""
    with pytest.raises(ConfigError, match="absolute path"):
        load_config(write_config(toml))


def test_command_empty_command_list_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[commands.bad]
group = "test"
command = []
timeout = 10
"""
    with pytest.raises(ConfigError, match="non-empty"):
        load_config(write_config(toml))


def test_command_negative_timeout_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[commands.bad]
group = "test"
command = ["/bin/true"]
timeout = -1
"""
    with pytest.raises(ConfigError, match="positive"):
        load_config(write_config(toml))


def test_command_zero_timeout_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[commands.bad]
group = "test"
command = ["/bin/true"]
timeout = 0
"""
    with pytest.raises(ConfigError, match="positive"):
        load_config(write_config(toml))


def test_command_wrong_type_requires_confirmation_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[commands.bad]
group = "test"
command = ["/bin/true"]
timeout = 10
requires_confirmation = "yes"
"""
    with pytest.raises(ConfigError, match="bool"):
        load_config(write_config(toml))


def test_command_wrong_type_description_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[commands.bad]
group = "test"
command = ["/bin/true"]
timeout = 10
description = 42
"""
    with pytest.raises(ConfigError, match="string"):
        load_config(write_config(toml))


def test_command_non_string_in_command_list_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[commands.bad]
group = "test"
command = ["/bin/true", 42]
timeout = 10
"""
    with pytest.raises(ConfigError, match="string"):
        load_config(write_config(toml))


def test_command_empty_group_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[commands.bad]
group = ""
command = ["/bin/true"]
timeout = 10
"""
    with pytest.raises(ConfigError, match="empty"):
        load_config(write_config(toml))


# ---------------------------------------------------------------------------
# Disabled commands
# ---------------------------------------------------------------------------


def test_disabled_commands_default_empty(write_config: Callable[[str], Path]) -> None:
    config = load_config(write_config(MINIMAL_VALID))
    assert config.agent.disabled_commands == frozenset()


def test_disabled_commands_parsed(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace(
        'pulse_token = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"',
        'pulse_token = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"\n'
        'disabled_commands = ["git_pull", "docker_logs"]',
    )
    config = load_config(write_config(content))
    assert config.agent.disabled_commands == frozenset({"git_pull", "docker_logs"})


def test_disabled_commands_empty_list(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace(
        'pulse_token = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"',
        'pulse_token = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"\n'
        'disabled_commands = []',
    )
    config = load_config(write_config(content))
    assert config.agent.disabled_commands == frozenset()


def test_disabled_commands_wrong_type_raises(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace(
        'pulse_token = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"',
        'pulse_token = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"\n'
        'disabled_commands = "git_pull"',
    )
    with pytest.raises(ConfigError, match="list"):
        load_config(write_config(content))


def test_disabled_commands_non_string_item_raises(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace(
        'pulse_token = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"',
        'pulse_token = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"\n'
        'disabled_commands = ["git_pull", 42]',
    )
    with pytest.raises(ConfigError, match="string"):
        load_config(write_config(content))


def test_disabled_commands_is_frozenset(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace(
        'pulse_token = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"',
        'pulse_token = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"\n'
        'disabled_commands = ["git_pull"]',
    )
    config = load_config(write_config(content))
    assert isinstance(config.agent.disabled_commands, frozenset)


# ---------------------------------------------------------------------------
# Command params (ParamDef)
# ---------------------------------------------------------------------------

COMMAND_WITH_PARAMS_TOML = """
[commands.docker_logs_service]
group = "diagnostics"
command = ["/usr/bin/docker", "compose", "-f", "{compose_file}", "logs", "--tail", "100", "{service}"]
timeout = 30
description = "Show logs for a specific service"

[commands.docker_logs_service.params.service]
placeholder = "service"
default = "web"
pattern = "[a-zA-Z0-9_-]+"
description = "Docker Compose service name"
"""


def test_command_params_parsed(write_config: Callable[[str], Path]) -> None:
    config = load_config(write_config(MINIMAL_VALID + COMMAND_WITH_PARAMS_TOML))
    cmd = config.commands["docker_logs_service"]
    assert "service" in cmd.params
    p = cmd.params["service"]
    assert p.placeholder == "service"
    assert p.default == "web"
    assert p.pattern == "[a-zA-Z0-9_-]+"
    assert p.description == "Docker Compose service name"


def test_command_params_omitted_is_empty_dict(write_config: Callable[[str], Path]) -> None:
    config = load_config(write_config(MINIMAL_VALID + CUSTOM_COMMAND_TOML))
    cmd = config.commands["restart_caddy"]
    assert cmd.params == {}


def test_command_params_none_default(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[commands.logs]
group = "diagnostics"
command = ["/usr/bin/docker", "logs", "{service}"]
timeout = 30

[commands.logs.params.service]
placeholder = "service"
pattern = "[a-zA-Z0-9_-]+"
"""
    config = load_config(write_config(toml))
    p = config.commands["logs"].params["service"]
    assert p.default is None


def test_command_params_protected_placeholder_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[commands.bad]
group = "test"
command = ["/bin/true", "{project_dir}"]
timeout = 10

[commands.bad.params.project_dir]
placeholder = "project_dir"
default = "/hacked"
pattern = ".*"
"""
    with pytest.raises(ConfigError, match="protected"):
        load_config(write_config(toml))


def test_command_params_invalid_regex_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[commands.bad]
group = "test"
command = ["/bin/true", "{svc}"]
timeout = 10

[commands.bad.params.svc]
placeholder = "svc"
default = "web"
pattern = "[invalid("
"""
    with pytest.raises(ConfigError, match="regex"):
        load_config(write_config(toml))


def test_command_params_missing_placeholder_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[commands.bad]
group = "test"
command = ["/bin/true"]
timeout = 10

[commands.bad.params.svc]
default = "web"
pattern = ".*"
"""
    with pytest.raises(ConfigError, match="placeholder"):
        load_config(write_config(toml))


def test_command_params_missing_pattern_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[commands.bad]
group = "test"
command = ["/bin/true"]
timeout = 10

[commands.bad.params.svc]
placeholder = "svc"
default = "web"
"""
    with pytest.raises(ConfigError, match="pattern"):
        load_config(write_config(toml))


def test_command_params_wrong_type_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[commands.bad]
group = "test"
command = ["/bin/true"]
timeout = 10
params = "not a table"
"""
    with pytest.raises(ConfigError, match="table"):
        load_config(write_config(toml))


def test_command_params_placeholder_mismatch_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[commands.bad]
group = "test"
command = ["/bin/true", "{svc}"]
timeout = 10

[commands.bad.params.svc]
placeholder = "different_name"
default = "web"
pattern = ".*"
"""
    with pytest.raises(ConfigError, match="match"):
        load_config(write_config(toml))


# ---------------------------------------------------------------------------
# log_groups
# ---------------------------------------------------------------------------


_VALID_LOG_GROUP = """
[[log_groups]]
name = "storage"
enabled = true
source_type = "file"
source_path = "/var/log/garage/garaged.log"
filter_contains = "garage_api_common"
parser = "garage_s3"
ship_interval_seconds = 10
max_lines_per_batch = 200
retention_days = 90
"""


def test_log_groups_empty_when_absent(write_config: Callable[[str], Path]) -> None:
    cfg = load_config(write_config(MINIMAL_VALID))
    assert cfg.log_groups == []


def test_log_groups_parsed(write_config: Callable[[str], Path]) -> None:
    cfg = load_config(write_config(MINIMAL_VALID + _VALID_LOG_GROUP))
    assert len(cfg.log_groups) == 1
    g = cfg.log_groups[0]
    assert g.name == "storage"
    assert g.enabled is True
    assert g.parser == "garage_s3"
    assert g.ship_interval_seconds == 10.0
    assert g.max_lines_per_batch == 200
    assert g.retention_days == 90


def test_log_groups_duplicate_name_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + _VALID_LOG_GROUP + _VALID_LOG_GROUP
    with pytest.raises(ConfigError, match="Duplicate"):
        load_config(write_config(toml))


def test_log_groups_invalid_name_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[[log_groups]]
name = "bad name with spaces"
enabled = true
source_type = "file"
source_path = "/var/log/x.log"
parser = "stormpulse"
ship_interval_seconds = 10
max_lines_per_batch = 200
retention_days = 90
"""
    with pytest.raises(ConfigError, match="alphanumeric"):
        load_config(write_config(toml))


def test_log_groups_non_file_source_type_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[[log_groups]]
name = "x"
enabled = true
source_type = "syslog"
source_path = "/var/log/x.log"
parser = "stormpulse"
ship_interval_seconds = 10
max_lines_per_batch = 200
retention_days = 90
"""
    with pytest.raises(ConfigError, match="'source_type'"):
        load_config(write_config(toml))


def test_log_groups_relative_path_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[[log_groups]]
name = "x"
enabled = true
source_type = "file"
source_path = "relative/path.log"
parser = "stormpulse"
ship_interval_seconds = 10
max_lines_per_batch = 200
retention_days = 90
"""
    with pytest.raises(ConfigError, match="absolute"):
        load_config(write_config(toml))


def test_log_groups_unknown_parser_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[[log_groups]]
name = "x"
enabled = true
source_type = "file"
source_path = "/var/log/x.log"
parser = "made_up"
ship_interval_seconds = 10
max_lines_per_batch = 200
retention_days = 90
"""
    with pytest.raises(ConfigError, match="'parser'"):
        load_config(write_config(toml))


def test_log_groups_interval_too_low_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[[log_groups]]
name = "x"
enabled = true
source_type = "file"
source_path = "/var/log/x.log"
parser = "stormpulse"
ship_interval_seconds = 1
max_lines_per_batch = 200
retention_days = 90
"""
    with pytest.raises(ConfigError, match="ship_interval_seconds"):
        load_config(write_config(toml))


def test_log_groups_batch_too_large_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[[log_groups]]
name = "x"
enabled = true
source_type = "file"
source_path = "/var/log/x.log"
parser = "stormpulse"
ship_interval_seconds = 10
max_lines_per_batch = 500
retention_days = 90
"""
    with pytest.raises(ConfigError, match="max_lines_per_batch"):
        load_config(write_config(toml))


def test_log_groups_retention_out_of_range_raises(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[[log_groups]]
name = "x"
enabled = true
source_type = "file"
source_path = "/var/log/x.log"
parser = "stormpulse"
ship_interval_seconds = 10
max_lines_per_batch = 200
retention_days = 0
"""
    with pytest.raises(ConfigError, match="retention_days"):
        load_config(write_config(toml))


# ---------------------------------------------------------------------------
# [garage] section
# ---------------------------------------------------------------------------


_VALID_GARAGE = """
[garage]
enabled = true
container_name = "garaged"
garage_binary = "/garage"
docker_binary = "/usr/bin/docker"
config_path = "/etc/garage/garage.toml"
state_push_interval_seconds = 300
"""


def test_garage_section_parses(write_config: Callable[[str], Path]) -> None:
    cfg = load_config(write_config(MINIMAL_VALID + _VALID_GARAGE))
    assert cfg.garage is not None
    assert cfg.garage.enabled is True
    assert cfg.garage.container_name == "garaged"
    assert cfg.garage.state_push_interval_seconds == 300.0


def test_garage_section_absent_is_none(write_config: Callable[[str], Path]) -> None:
    cfg = load_config(write_config(MINIMAL_VALID))
    assert cfg.garage is None


def test_garage_must_be_table(write_config: Callable[[str], Path]) -> None:
    toml = 'garage = "oops"\n' + MINIMAL_VALID
    with pytest.raises(ConfigError, match=r"\[garage\] must be a table"):
        load_config(write_config(toml))


def test_garage_empty_container_name(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + _VALID_GARAGE.replace(
        'container_name = "garaged"', 'container_name = ""',
    )
    with pytest.raises(ConfigError, match="container_name.*must not be empty"):
        load_config(write_config(toml))


def test_garage_empty_binary(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + _VALID_GARAGE.replace(
        'garage_binary = "/garage"', 'garage_binary = ""',
    )
    with pytest.raises(ConfigError, match="garage_binary.*must not be empty"):
        load_config(write_config(toml))


def test_garage_docker_binary_must_be_absolute(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + _VALID_GARAGE.replace(
        'docker_binary = "/usr/bin/docker"', 'docker_binary = "docker"',
    )
    with pytest.raises(ConfigError, match="docker_binary.*absolute path"):
        load_config(write_config(toml))


def test_garage_interval_must_be_positive(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + _VALID_GARAGE.replace(
        "state_push_interval_seconds = 300", "state_push_interval_seconds = 0",
    )
    with pytest.raises(ConfigError, match="state_push_interval_seconds.*positive"):
        load_config(write_config(toml))


# ---------------------------------------------------------------------------
# log_groups - non-array + filter_contains type
# ---------------------------------------------------------------------------


def test_log_groups_must_be_array(write_config: Callable[[str], Path]) -> None:
    toml = 'log_groups = "not an array"\n' + MINIMAL_VALID
    with pytest.raises(ConfigError, match="log_groups.*must be an array"):
        load_config(write_config(toml))


def test_log_groups_filter_contains_must_be_string(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + """
[[log_groups]]
name = "x"
enabled = true
source_type = "file"
source_path = "/var/log/x.log"
parser = "stormpulse"
ship_interval_seconds = 10
max_lines_per_batch = 100
retention_days = 30
filter_contains = 42
"""
    with pytest.raises(ConfigError, match="filter_contains"):
        load_config(write_config(toml))


def test_log_groups_filter_contains_defaults_to_empty(
    write_config: Callable[[str], Path],
) -> None:
    toml = MINIMAL_VALID + """
[[log_groups]]
name = "x"
enabled = true
source_type = "file"
source_path = "/var/log/x.log"
parser = "stormpulse"
ship_interval_seconds = 10
max_lines_per_batch = 100
retention_days = 30
"""
    cfg = load_config(write_config(toml))
    assert cfg.log_groups[0].filter_contains == ""


def test_log_groups_ship_interval_boundary_5_accepted(
    write_config: Callable[[str], Path],
) -> None:
    toml = MINIMAL_VALID + """
[[log_groups]]
name = "x"
enabled = true
source_type = "file"
source_path = "/var/log/x.log"
parser = "stormpulse"
ship_interval_seconds = 5
max_lines_per_batch = 200
retention_days = 365
"""
    cfg = load_config(write_config(toml))
    assert cfg.log_groups[0].ship_interval_seconds == 5.0
    assert cfg.log_groups[0].max_lines_per_batch == 200
    assert cfg.log_groups[0].retention_days == 365
