"""Tests for stormpulse.config."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import pytest

from stormpulse.config import CommandSpec, ConfigError, load_config
from stormpulse.garage.config import GarageConfig, parse_garage_config

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


@pytest.mark.parametrize(
    "section",
    [
        "agent",
        "dashboard",
        "tls",
        "auth",
        "metrics",
        "project",
        "storage",
    ],
)
def test_missing_section_raises(
    write_config: Callable[[str], Path], section: str
) -> None:
    lines = [
        line
        for line in MINIMAL_VALID.splitlines(keepends=True)
        if not line.strip().startswith(f"[{section}]")
    ]
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


def test_wrong_type_for_collect_containers_raises(
    write_config: Callable[[str], Path],
) -> None:
    content = MINIMAL_VALID.replace(
        "collect_containers = false", 'collect_containers = "yes"'
    )
    with pytest.raises(ConfigError, match="bool"):
        load_config(write_config(content))


# ---------------------------------------------------------------------------
# Range validation
# ---------------------------------------------------------------------------


def test_negative_reconnect_raises(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace(
        "reconnect_min_seconds = 1", "reconnect_min_seconds = -1"
    )
    with pytest.raises(ConfigError, match="positive"):
        load_config(write_config(content))


def test_zero_reconnect_raises(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace(
        "reconnect_min_seconds = 1", "reconnect_min_seconds = 0"
    )
    with pytest.raises(ConfigError, match="positive"):
        load_config(write_config(content))


def test_min_greater_than_max_raises(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace(
        "reconnect_min_seconds = 1", "reconnect_min_seconds = 100"
    )
    with pytest.raises(ConfigError, match="<="):
        load_config(write_config(content))


def test_negative_max_age_raises(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace(
        "command_max_age_seconds = 60", "command_max_age_seconds = -1"
    )
    with pytest.raises(ConfigError, match="positive"):
        load_config(write_config(content))


def test_zero_max_age_raises(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace(
        "command_max_age_seconds = 60", "command_max_age_seconds = 0"
    )
    with pytest.raises(ConfigError, match="positive"):
        load_config(write_config(content))


def test_negative_push_interval_raises(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace(
        "push_interval_seconds = 10", "push_interval_seconds = 0"
    )
    with pytest.raises(ConfigError, match="positive"):
        load_config(write_config(content))


def test_zero_heartbeat_interval_raises(write_config: Callable[[str], Path]) -> None:
    content = MINIMAL_VALID.replace(
        "heartbeat_interval_seconds = 30", "heartbeat_interval_seconds = 0"
    )
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


def test_no_commands_section_gives_empty_dict(
    write_config: Callable[[str], Path],
) -> None:
    config = load_config(write_config(MINIMAL_VALID))
    assert config.commands == {}


def test_custom_command_parsed(write_config: Callable[[str], Path]) -> None:
    config = load_config(write_config(MINIMAL_VALID + CUSTOM_COMMAND_TOML))
    assert "restart_caddy" in config.commands
    cmd = config.commands["restart_caddy"]
    assert isinstance(cmd, CommandSpec)
    assert cmd.group == "maintenance"
    assert cmd.command == ["/usr/bin/systemctl", "restart", "caddy.service"]
    assert cmd.timeout == 30
    assert cmd.requires_confirmation is True
    assert cmd.description == "Restart Caddy reverse proxy"


def test_custom_command_defaults(write_config: Callable[[str], Path]) -> None:
    toml = (
        MINIMAL_VALID
        + """
[commands.simple]
group = "test"
command = ["/bin/true"]
timeout = 5
"""
    )
    config = load_config(write_config(toml))
    cmd = config.commands["simple"]
    assert cmd.requires_confirmation is False
    assert cmd.description == ""
    assert cmd.long_running is False


def test_custom_command_long_running_rejected(
    write_config: Callable[[str], Path],
) -> None:
    # Config-defined commands are subprocess-only: a job command's handler can
    # only be contributed by an integration, so 'long_running' in config is
    # refused at load rather than silently producing a command that fails at
    # dispatch (no handler).
    toml = (
        MINIMAL_VALID
        + """
[commands.bulk_op]
group = "maintenance"
command = ["/usr/bin/true"]
timeout = 600
long_running = true
"""
    )
    with pytest.raises(ConfigError, match="long_running"):
        load_config(write_config(toml))


def test_command_spec_job_requires_handler() -> None:
    # The single-source guarantee made structural: a job with no handler is the
    # half-registration footgun, and it must be impossible to even construct.
    with pytest.raises(ValueError, match="job"):
        CommandSpec(group="x", command=["x"], timeout=5, mode="job")


def test_command_spec_non_job_rejects_handler() -> None:
    with pytest.raises(ValueError, match="handler"):
        CommandSpec(
            group="x",
            command=["/bin/true"],
            timeout=5,
            mode="subprocess",
            handler=lambda _p: None,
        )


def test_command_spec_subprocess_requires_absolute_path() -> None:
    # The Layer-4 whitelist invariant, enforced at construction instead of by a
    # hand-maintained skip-list of exempt command names.
    with pytest.raises(ValueError, match="absolute"):
        CommandSpec(group="x", command=["relative-binary"], timeout=5)


def test_command_spec_long_running_derived_from_mode() -> None:
    job = CommandSpec(
        group="x", command=["x"], timeout=5, mode="job", handler=lambda _p: None
    )
    sub = CommandSpec(group="x", command=["/bin/true"], timeout=5)
    assert job.long_running is True
    assert sub.long_running is False


def test_custom_command_long_running_wrong_type_raises(
    write_config: Callable[[str], Path],
) -> None:
    toml = (
        MINIMAL_VALID
        + """
[commands.bad]
group = "x"
command = ["/bin/true"]
timeout = 10
long_running = "yes"
"""
    )
    with pytest.raises(ConfigError, match="long_running"):
        load_config(write_config(toml))


def test_multiple_custom_commands(write_config: Callable[[str], Path]) -> None:
    toml = (
        MINIMAL_VALID
        + """
[commands.cmd_a]
group = "a"
command = ["/bin/true"]
timeout = 10

[commands.cmd_b]
group = "b"
command = ["/bin/false"]
timeout = 20
"""
    )
    config = load_config(write_config(toml))
    assert len(config.commands) == 2
    assert "cmd_a" in config.commands
    assert "cmd_b" in config.commands


def test_command_missing_group_raises(write_config: Callable[[str], Path]) -> None:
    toml = (
        MINIMAL_VALID
        + """
[commands.bad]
command = ["/bin/true"]
timeout = 10
"""
    )
    with pytest.raises(ConfigError, match="group"):
        load_config(write_config(toml))


def test_command_missing_command_raises(write_config: Callable[[str], Path]) -> None:
    toml = (
        MINIMAL_VALID
        + """
[commands.bad]
group = "test"
timeout = 10
"""
    )
    with pytest.raises(ConfigError, match="command"):
        load_config(write_config(toml))


def test_command_missing_timeout_raises(write_config: Callable[[str], Path]) -> None:
    toml = (
        MINIMAL_VALID
        + """
[commands.bad]
group = "test"
command = ["/bin/true"]
"""
    )
    with pytest.raises(ConfigError, match="timeout"):
        load_config(write_config(toml))


def test_command_bool_timeout_raises(write_config: Callable[[str], Path]) -> None:
    """A boolean timeout must be rejected; bool is an int subclass in Python."""
    toml = (
        MINIMAL_VALID
        + """
[commands.bad]
group = "test"
command = ["/bin/true"]
timeout = true
"""
    )
    with pytest.raises(ConfigError, match="timeout"):
        load_config(write_config(toml))


def test_command_non_absolute_path_raises(write_config: Callable[[str], Path]) -> None:
    toml = (
        MINIMAL_VALID
        + """
[commands.bad]
group = "test"
command = ["relative/bin", "arg"]
timeout = 10
"""
    )
    with pytest.raises(ConfigError, match="absolute path"):
        load_config(write_config(toml))


def test_command_empty_command_list_raises(write_config: Callable[[str], Path]) -> None:
    toml = (
        MINIMAL_VALID
        + """
[commands.bad]
group = "test"
command = []
timeout = 10
"""
    )
    with pytest.raises(ConfigError, match="non-empty"):
        load_config(write_config(toml))


def test_command_negative_timeout_raises(write_config: Callable[[str], Path]) -> None:
    toml = (
        MINIMAL_VALID
        + """
[commands.bad]
group = "test"
command = ["/bin/true"]
timeout = -1
"""
    )
    with pytest.raises(ConfigError, match="positive"):
        load_config(write_config(toml))


def test_command_zero_timeout_raises(write_config: Callable[[str], Path]) -> None:
    toml = (
        MINIMAL_VALID
        + """
[commands.bad]
group = "test"
command = ["/bin/true"]
timeout = 0
"""
    )
    with pytest.raises(ConfigError, match="positive"):
        load_config(write_config(toml))


def test_command_wrong_type_requires_confirmation_raises(
    write_config: Callable[[str], Path],
) -> None:
    toml = (
        MINIMAL_VALID
        + """
[commands.bad]
group = "test"
command = ["/bin/true"]
timeout = 10
requires_confirmation = "yes"
"""
    )
    with pytest.raises(ConfigError, match="bool"):
        load_config(write_config(toml))


def test_command_wrong_type_description_raises(
    write_config: Callable[[str], Path],
) -> None:
    toml = (
        MINIMAL_VALID
        + """
[commands.bad]
group = "test"
command = ["/bin/true"]
timeout = 10
description = 42
"""
    )
    with pytest.raises(ConfigError, match="string"):
        load_config(write_config(toml))


def test_command_non_string_in_command_list_raises(
    write_config: Callable[[str], Path],
) -> None:
    toml = (
        MINIMAL_VALID
        + """
[commands.bad]
group = "test"
command = ["/bin/true", 42]
timeout = 10
"""
    )
    with pytest.raises(ConfigError, match="string"):
        load_config(write_config(toml))


def test_command_empty_group_raises(write_config: Callable[[str], Path]) -> None:
    toml = (
        MINIMAL_VALID
        + """
[commands.bad]
group = ""
command = ["/bin/true"]
timeout = 10
"""
    )
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
        'pulse_token = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"\ndisabled_commands = []',
    )
    config = load_config(write_config(content))
    assert config.agent.disabled_commands == frozenset()


def test_disabled_commands_wrong_type_raises(
    write_config: Callable[[str], Path],
) -> None:
    content = MINIMAL_VALID.replace(
        'pulse_token = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"',
        'pulse_token = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"\n'
        'disabled_commands = "git_pull"',
    )
    with pytest.raises(ConfigError, match="list"):
        load_config(write_config(content))


def test_disabled_commands_non_string_item_raises(
    write_config: Callable[[str], Path],
) -> None:
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


def test_command_params_omitted_is_empty_dict(
    write_config: Callable[[str], Path],
) -> None:
    config = load_config(write_config(MINIMAL_VALID + CUSTOM_COMMAND_TOML))
    cmd = config.commands["restart_caddy"]
    assert cmd.params == {}


def test_command_params_none_default(write_config: Callable[[str], Path]) -> None:
    toml = (
        MINIMAL_VALID
        + """
[commands.logs]
group = "diagnostics"
command = ["/usr/bin/docker", "logs", "{service}"]
timeout = 30

[commands.logs.params.service]
placeholder = "service"
pattern = "[a-zA-Z0-9_-]+"
"""
    )
    config = load_config(write_config(toml))
    p = config.commands["logs"].params["service"]
    assert p.default is None


def test_command_params_protected_placeholder_raises(
    write_config: Callable[[str], Path],
) -> None:
    toml = (
        MINIMAL_VALID
        + """
[commands.bad]
group = "test"
command = ["/bin/true", "{project_dir}"]
timeout = 10

[commands.bad.params.project_dir]
placeholder = "project_dir"
default = "/hacked"
pattern = ".*"
"""
    )
    with pytest.raises(ConfigError, match="protected"):
        load_config(write_config(toml))


def test_command_params_invalid_regex_raises(
    write_config: Callable[[str], Path],
) -> None:
    toml = (
        MINIMAL_VALID
        + """
[commands.bad]
group = "test"
command = ["/bin/true", "{svc}"]
timeout = 10

[commands.bad.params.svc]
placeholder = "svc"
default = "web"
pattern = "[invalid("
"""
    )
    with pytest.raises(ConfigError, match="regex"):
        load_config(write_config(toml))


def test_command_params_missing_placeholder_raises(
    write_config: Callable[[str], Path],
) -> None:
    toml = (
        MINIMAL_VALID
        + """
[commands.bad]
group = "test"
command = ["/bin/true"]
timeout = 10

[commands.bad.params.svc]
default = "web"
pattern = ".*"
"""
    )
    with pytest.raises(ConfigError, match="placeholder"):
        load_config(write_config(toml))


def test_command_params_missing_pattern_raises(
    write_config: Callable[[str], Path],
) -> None:
    toml = (
        MINIMAL_VALID
        + """
[commands.bad]
group = "test"
command = ["/bin/true"]
timeout = 10

[commands.bad.params.svc]
placeholder = "svc"
default = "web"
"""
    )
    with pytest.raises(ConfigError, match="pattern"):
        load_config(write_config(toml))


def test_command_params_wrong_type_raises(write_config: Callable[[str], Path]) -> None:
    toml = (
        MINIMAL_VALID
        + """
[commands.bad]
group = "test"
command = ["/bin/true"]
timeout = 10
params = "not a table"
"""
    )
    with pytest.raises(ConfigError, match="table"):
        load_config(write_config(toml))


def test_command_params_placeholder_mismatch_raises(
    write_config: Callable[[str], Path],
) -> None:
    toml = (
        MINIMAL_VALID
        + """
[commands.bad]
group = "test"
command = ["/bin/true", "{svc}"]
timeout = 10

[commands.bad.params.svc]
placeholder = "different_name"
default = "web"
pattern = ".*"
"""
    )
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


# A malformed individual log group is SKIPPED with a warning, not fatal
# (degrade-don't-crash, 2026-06-06). The valid groups still load; the bad one is
# dropped and its reason is logged. Only `log_groups` not being an array is fatal
# (see test_log_groups_must_be_array). Each test below asserts the bad group did
# not load AND that the specific validation reason was warned.


def test_log_groups_duplicate_name_skipped(
    write_config: Callable[[str], Path], caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    toml = MINIMAL_VALID + _VALID_LOG_GROUP + _VALID_LOG_GROUP
    cfg = load_config(write_config(toml))
    assert len(cfg.log_groups) == 1  # first kept, duplicate dropped
    assert "Duplicate" in caplog.text


def test_log_groups_invalid_name_skipped(
    write_config: Callable[[str], Path], caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    toml = (
        MINIMAL_VALID
        + """
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
    )
    cfg = load_config(write_config(toml))
    assert cfg.log_groups == []
    assert "alphanumeric" in caplog.text


def test_log_groups_non_file_source_type_skipped(
    write_config: Callable[[str], Path], caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    toml = (
        MINIMAL_VALID
        + """
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
    )
    cfg = load_config(write_config(toml))
    assert cfg.log_groups == []
    assert "source_type" in caplog.text


def test_log_groups_relative_path_skipped(
    write_config: Callable[[str], Path], caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    toml = (
        MINIMAL_VALID
        + """
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
    )
    cfg = load_config(write_config(toml))
    assert cfg.log_groups == []
    assert "absolute" in caplog.text


def test_log_groups_file_source_missing_source_path_skipped(
    write_config: Callable[[str], Path], caplog: pytest.LogCaptureFixture,
) -> None:
    """The 2026-06-06 incident: the caddy log offer wrote `path` instead of
    `source_path`. A file group missing `source_path` must skip (warned), not
    crash-loop the whole agent."""
    caplog.set_level(logging.WARNING)
    toml = (
        MINIMAL_VALID
        + """
[[log_groups]]
name = "caddy"
enabled = true
source_type = "file"
path = "/var/log/caddy/access.log"
parser = "caddy_json"
ship_interval_seconds = 10
max_lines_per_batch = 200
retention_days = 90
"""
    )
    cfg = load_config(write_config(toml))
    assert cfg.log_groups == []
    assert "source_path" in caplog.text


def test_log_groups_unknown_parser_skipped(
    write_config: Callable[[str], Path], caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    toml = (
        MINIMAL_VALID
        + """
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
    )
    cfg = load_config(write_config(toml))
    assert cfg.log_groups == []
    assert "parser" in caplog.text


def test_log_groups_one_bad_one_good_keeps_good(
    write_config: Callable[[str], Path], caplog: pytest.LogCaptureFixture,
) -> None:
    """A bad group does not take the valid ones down with it."""
    caplog.set_level(logging.WARNING)
    bad = """
[[log_groups]]
name = "broken"
enabled = true
source_type = "file"
parser = "stormpulse"
ship_interval_seconds = 10
max_lines_per_batch = 200
retention_days = 90
"""
    cfg = load_config(write_config(MINIMAL_VALID + _VALID_LOG_GROUP + bad))
    assert [g.name for g in cfg.log_groups] == ["storage"]
    assert "broken" in caplog.text or "index 1" in caplog.text


def test_log_groups_interval_too_low_skipped(
    write_config: Callable[[str], Path], caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    toml = (
        MINIMAL_VALID
        + """
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
    )
    cfg = load_config(write_config(toml))
    assert cfg.log_groups == []
    assert "ship_interval_seconds" in caplog.text


def test_log_groups_interval_two_seconds_accepted(
    write_config: Callable[[str], Path],
) -> None:
    """2s is the floor: it lets the activity feed keep pace with the 2s metrics
    push. Anything below 2 still raises (see the too-low test above)."""
    toml = (
        MINIMAL_VALID
        + """
[[log_groups]]
name = "storage"
enabled = true
source_type = "file"
source_path = "/var/log/garage/garaged.log"
filter_contains = "garage_api_common"
parser = "garage_s3"
ship_interval_seconds = 2
max_lines_per_batch = 200
retention_days = 90
"""
    )
    cfg = load_config(write_config(toml))
    assert cfg.log_groups[0].ship_interval_seconds == 2.0


def test_log_groups_batch_too_large_skipped(
    write_config: Callable[[str], Path], caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    toml = (
        MINIMAL_VALID
        + """
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
    )
    cfg = load_config(write_config(toml))
    assert cfg.log_groups == []
    assert "max_lines_per_batch" in caplog.text


def test_log_groups_retention_out_of_range_skipped(
    write_config: Callable[[str], Path], caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    toml = (
        MINIMAL_VALID
        + """
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
    )
    cfg = load_config(write_config(toml))
    assert cfg.log_groups == []
    assert "retention_days" in caplog.text


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
"""


# CORE-005: Foundation only captures the raw [garage] table into
# config.integrations; the typed parse/validation lives in garage/config.py.
def _parse_garage(write_config: Callable[[str], Path], toml: str) -> GarageConfig:
    return parse_garage_config(load_config(write_config(toml)).integrations["garage"])


def test_garage_section_parses(write_config: Callable[[str], Path]) -> None:
    gc = _parse_garage(write_config, MINIMAL_VALID + _VALID_GARAGE)
    assert gc.enabled is True
    assert gc.container_name == "garaged"


def test_garage_admin_token_file_is_read_and_stripped(
    write_config: Callable[[str], Path], tmp_path: Path,
) -> None:
    tok = tmp_path / "admin_token"
    tok.write_text("s3cr3t\n")
    toml = MINIMAL_VALID + _VALID_GARAGE + (
        'admin_url = "http://127.0.0.1:3903"\n'
        f'admin_token_file = "{tok}"\n'
    )
    gc = _parse_garage(write_config, toml)
    assert gc.admin_url == "http://127.0.0.1:3903"
    assert gc.admin_token == "s3cr3t"


def test_garage_admin_inline_token(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + _VALID_GARAGE + (
        'admin_url = "http://127.0.0.1:3903"\n'
        'admin_token = "inline-tok"\n'
    )
    gc = _parse_garage(write_config, toml)
    assert gc.admin_token == "inline-tok"


def test_garage_unreadable_admin_token_file_degrades_not_crashes(
    write_config: Callable[[str], Path], tmp_path: Path,
) -> None:
    # REGRESSION: an unreadable admin_token_file used to raise ConfigError, which
    # crash-looped the whole agent. It must degrade to the admin API disabled
    # (admin_token = "") and leave the rest of the config intact.
    missing = tmp_path / "nope" / "admin_token"
    toml = MINIMAL_VALID + _VALID_GARAGE + (
        'admin_url = "http://127.0.0.1:3903"\n'
        f'admin_token_file = "{missing}"\n'
    )
    gc = _parse_garage(write_config, toml)  # must NOT raise
    assert gc.admin_url == "http://127.0.0.1:3903"
    assert gc.admin_token == ""


def test_garage_section_absent_not_in_integrations(
    write_config: Callable[[str], Path],
) -> None:
    cfg = load_config(write_config(MINIMAL_VALID))
    assert "garage" not in cfg.integrations


def test_garage_non_table_is_ignored(write_config: Callable[[str], Path]) -> None:
    # CORE-005: Foundation no longer validates integration sections. A non-table
    # ``garage`` value is simply not captured as an integration (no registered
    # integration claims it), rather than raising at load time.
    toml = 'garage = "oops"\n' + MINIMAL_VALID
    cfg = load_config(write_config(toml))
    assert "garage" not in cfg.integrations


def test_garage_empty_container_name(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + _VALID_GARAGE.replace(
        'container_name = "garaged"',
        'container_name = ""',
    )
    with pytest.raises(ConfigError, match="container_name.*must not be empty"):
        _parse_garage(write_config, toml)


def test_garage_empty_binary(write_config: Callable[[str], Path]) -> None:
    toml = MINIMAL_VALID + _VALID_GARAGE.replace(
        'garage_binary = "/garage"',
        'garage_binary = ""',
    )
    with pytest.raises(ConfigError, match="garage_binary.*must not be empty"):
        _parse_garage(write_config, toml)


def test_garage_docker_binary_must_be_absolute(
    write_config: Callable[[str], Path],
) -> None:
    toml = MINIMAL_VALID + _VALID_GARAGE.replace(
        'docker_binary = "/usr/bin/docker"',
        'docker_binary = "docker"',
    )
    with pytest.raises(ConfigError, match="docker_binary.*absolute path"):
        _parse_garage(write_config, toml)


def test_garage_legacy_state_push_interval_ignored(
    write_config: Callable[[str], Path],
) -> None:
    # The state_push_interval_seconds knob was removed (the state read no longer
    # has a tunable interval). A deployed TOML still carrying it - any value, even
    # a once-invalid one - must update cleanly, so the key is ignored, not
    # rejected: parse succeeds and the config carries no such field.
    toml = MINIMAL_VALID + _VALID_GARAGE + "state_push_interval_seconds = 0\n"
    gc = _parse_garage(write_config, toml)
    assert gc.enabled is True
    assert not hasattr(gc, "state_push_interval_seconds")


# ---------------------------------------------------------------------------
# log_groups - non-array + filter_contains type
# ---------------------------------------------------------------------------


def test_log_groups_must_be_array(write_config: Callable[[str], Path]) -> None:
    toml = 'log_groups = "not an array"\n' + MINIMAL_VALID
    with pytest.raises(ConfigError, match="log_groups.*must be an array"):
        load_config(write_config(toml))


def test_log_groups_filter_contains_must_be_string_skipped(
    write_config: Callable[[str], Path], caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    toml = (
        MINIMAL_VALID
        + """
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
    )
    cfg = load_config(write_config(toml))
    assert cfg.log_groups == []
    assert "filter_contains" in caplog.text


def test_log_groups_filter_contains_defaults_to_empty(
    write_config: Callable[[str], Path],
) -> None:
    toml = (
        MINIMAL_VALID
        + """
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
    )
    cfg = load_config(write_config(toml))
    assert cfg.log_groups[0].filter_contains == ""


def test_log_groups_ship_interval_boundary_5_accepted(
    write_config: Callable[[str], Path],
) -> None:
    toml = (
        MINIMAL_VALID
        + """
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
    )
    cfg = load_config(write_config(toml))
    assert cfg.log_groups[0].ship_interval_seconds == 5.0
    assert cfg.log_groups[0].max_lines_per_batch == 200
    assert cfg.log_groups[0].retention_days == 365
