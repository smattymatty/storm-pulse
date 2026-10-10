"""Resolving a command's placeholders and running it as a subprocess: the
result shape per outcome, measured duration, and the job spec it refuses.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from stormpulse.commands import (
    COMMAND_REGISTRY,
    CommandError,
    CommandSpec,
    ParamValidationError,
    build_registry,
    execute_command,
    get_command,
    validate_params,
)
from stormpulse.commands.registry import _resolve_command
from stormpulse.config import ProjectConfig

# -- placeholder resolution --------------------------------------------------


def test_resolve_project_dir(project_config: ProjectConfig) -> None:
    template = ["/usr/bin/git", "-C", "{project_dir}", "pull"]
    resolved = _resolve_command(template, project_config)
    assert resolved == ["/usr/bin/git", "-C", "/opt/myapp", "pull"]


def test_resolve_compose_file(project_config: ProjectConfig) -> None:
    template = [
        "/usr/bin/docker",
        "compose",
        "--env-file",
        "{env_file}",
        "-f",
        "{compose_file}",
        "build",
    ]
    resolved = _resolve_command(template, project_config)
    assert resolved[3] == "/opt/myapp/.env"
    assert resolved[5] == "/opt/myapp/docker-compose.yml"


def test_resolve_docker_service_name(project_config: ProjectConfig) -> None:
    template = [
        "/usr/bin/docker",
        "compose",
        "--env-file",
        "{env_file}",
        "-f",
        "{compose_file}",
        "exec",
        "{docker_service_name}",
        "python",
    ]
    resolved = _resolve_command(template, project_config)
    assert resolved[7] == "web"


def test_resolve_unknown_placeholder_raises(project_config: ProjectConfig) -> None:
    template = ["/usr/bin/git", "-C", "{unknown_dir}", "pull"]
    with pytest.raises(ValueError, match="Unknown placeholder"):
        _resolve_command(template, project_config)


def test_resolve_env_file(project_config: ProjectConfig) -> None:
    template = [
        "/usr/bin/docker",
        "compose",
        "--env-file",
        "{env_file}",
        "-f",
        "{compose_file}",
        "up",
    ]
    resolved = _resolve_command(template, project_config)
    assert resolved == [
        "/usr/bin/docker",
        "compose",
        "--env-file",
        "/opt/myapp/.env",
        "-f",
        "/opt/myapp/docker-compose.yml",
        "up",
    ]


def test_resolve_without_env_file() -> None:
    """When env_file is None, --env-file and placeholder are stripped."""
    config = ProjectConfig(
        project_dir=Path("/opt/myapp"),
        compose_file=Path("/opt/myapp/docker-compose.yml"),
        docker_service_name="web",
    )
    template = [
        "/usr/bin/docker",
        "compose",
        "--env-file",
        "{env_file}",
        "-f",
        "{compose_file}",
        "up",
    ]
    resolved = _resolve_command(template, config)
    assert resolved == [
        "/usr/bin/docker",
        "compose",
        "-f",
        "/opt/myapp/docker-compose.yml",
        "up",
    ]


def test_resolve_strips_empty_flag_value() -> None:
    """Generic test: any --flag with empty value is stripped."""
    config = ProjectConfig(
        project_dir=Path("/opt/myapp"),
        compose_file=Path("/opt/myapp/docker-compose.yml"),
        docker_service_name="web",
    )
    template = [
        "/usr/bin/docker",
        "compose",
        "--env-file",
        "{env_file}",
        "-f",
        "{compose_file}",
        "build",
    ]
    resolved = _resolve_command(template, config)
    assert "--env-file" not in resolved
    assert "" not in resolved


def test_resolve_with_param_overrides(project_config: ProjectConfig) -> None:
    template = ["/usr/bin/docker", "logs", "{service}"]
    resolved = _resolve_command(template, project_config, {"service": "celery"})
    assert resolved == ["/usr/bin/docker", "logs", "celery"]


def test_resolve_param_overrides_alongside_config_placeholders(
    project_config: ProjectConfig,
) -> None:
    template = [
        "/usr/bin/docker",
        "compose",
        "-f",
        "{compose_file}",
        "logs",
        "{service}",
    ]
    resolved = _resolve_command(template, project_config, {"service": "worker"})
    assert resolved == [
        "/usr/bin/docker",
        "compose",
        "-f",
        "/opt/myapp/docker-compose.yml",
        "logs",
        "worker",
    ]


# -- execution ---------------------------------------------------------------


@patch("stormpulse.commands.registry.time.monotonic")
@patch("stormpulse.commands.registry.subprocess.run")
def test_execute_success(
    mock_run: MagicMock,
    mock_time: MagicMock,
    project_config: ProjectConfig,
) -> None:
    mock_time.side_effect = [0.0, 0.342]
    mock_run.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="Already up to date.\n",
        stderr="",
    )
    result = execute_command(
        "git_pull", project_config, "req-1", registry=COMMAND_REGISTRY
    )
    assert result.success is True
    assert result.exit_code == 0
    assert result.duration_ms == 342
    assert result.failure_reason is None
    assert result.stdout == "Already up to date.\n"


@patch("stormpulse.commands.registry.time.monotonic")
@patch("stormpulse.commands.registry.subprocess.run")
def test_execute_failure_nonzero_exit(
    mock_run: MagicMock,
    mock_time: MagicMock,
    project_config: ProjectConfig,
) -> None:
    mock_time.side_effect = [0.0, 1.0]
    mock_run.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="error: permission denied\n",
    )
    result = execute_command(
        "git_pull", project_config, "req-2", registry=COMMAND_REGISTRY
    )
    assert result.success is False
    assert result.exit_code == 1
    assert result.failure_reason == "exit_code"
    assert result.stderr == "error: permission denied\n"


@patch("stormpulse.commands.registry.time.monotonic")
@patch("stormpulse.commands.registry.subprocess.run")
def test_execute_timeout(
    mock_run: MagicMock,
    mock_time: MagicMock,
    project_config: ProjectConfig,
) -> None:
    mock_time.side_effect = [0.0, 60.0]
    exc = subprocess.TimeoutExpired(cmd="git", timeout=60)
    exc.stdout = "partial"  # type: ignore[assignment]
    exc.stderr = ""  # type: ignore[assignment]
    mock_run.side_effect = exc
    result = execute_command(
        "git_pull", project_config, "req-3", registry=COMMAND_REGISTRY
    )
    assert result.success is False
    assert result.exit_code == -1
    assert result.failure_reason == "timeout"
    assert result.stdout == "partial"
    assert "Command timed out after 60s" in result.stderr


@patch("stormpulse.commands.registry.time.monotonic")
@patch("stormpulse.commands.registry.subprocess.run")
def test_execute_binary_not_found(
    mock_run: MagicMock,
    mock_time: MagicMock,
    project_config: ProjectConfig,
) -> None:
    mock_time.side_effect = [0.0, 0.001]
    mock_run.side_effect = FileNotFoundError(
        "[Errno 2] No such file or directory: '/usr/bin/git'"
    )
    result = execute_command(
        "git_pull", project_config, "req-4", registry=COMMAND_REGISTRY
    )
    assert result.success is False
    assert result.exit_code == -1
    assert result.failure_reason == "not_found"
    assert "not found" in result.stderr.lower()


@patch("stormpulse.commands.registry.time.monotonic")
@patch("stormpulse.commands.registry.subprocess.run")
def test_execute_os_error(
    mock_run: MagicMock,
    mock_time: MagicMock,
    project_config: ProjectConfig,
) -> None:
    mock_time.side_effect = [0.0, 0.001]
    mock_run.side_effect = OSError("Permission denied")
    result = execute_command(
        "git_pull", project_config, "req-5", registry=COMMAND_REGISTRY
    )
    assert result.success is False
    assert result.exit_code == -1
    assert result.failure_reason == "os_error"


@patch("stormpulse.commands.registry.time.monotonic")
@patch("stormpulse.commands.registry.subprocess.run")
def test_execute_measures_duration(
    mock_run: MagicMock,
    mock_time: MagicMock,
    project_config: ProjectConfig,
) -> None:
    mock_time.side_effect = [1.0, 1.5]
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr=""
    )
    result = execute_command(
        "git_pull", project_config, "req-6", registry=COMMAND_REGISTRY
    )
    assert result.duration_ms == 500


@patch("stormpulse.commands.registry.time.monotonic")
@patch("stormpulse.commands.registry.subprocess.run")
def test_execute_forwards_sequence_id(
    mock_run: MagicMock,
    mock_time: MagicMock,
    project_config: ProjectConfig,
) -> None:
    mock_time.side_effect = [0.0, 0.1]
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr=""
    )
    result = execute_command(
        "git_pull",
        project_config,
        "req-7",
        sequence_id="seq-42",
        registry=COMMAND_REGISTRY,
    )
    assert result.sequence_id == "seq-42"


def test_execute_unknown_command_raises(project_config: ProjectConfig) -> None:
    with pytest.raises(CommandError, match="Unknown command"):
        execute_command("nope", project_config, "req-x", registry=COMMAND_REGISTRY)


@patch("stormpulse.commands.registry.subprocess.run")
def test_execute_command_refuses_job_spec(mock_run: MagicMock) -> None:
    """A job's bare name never reaches subprocess, where PATH would resolve it."""
    job = CommandSpec(
        group="g", command=["some_job"], timeout=5, mode="job", handler=lambda p: None
    )
    with pytest.raises(CommandError, match="job command"):
        execute_command("some_job", MagicMock(), "rid", registry={"some_job": job})
    mock_run.assert_not_called()


@patch("stormpulse.commands.registry.time.monotonic")
@patch("stormpulse.commands.registry.subprocess.run")
def test_execute_config_defined_command(
    mock_run: MagicMock,
    mock_time: MagicMock,
    project_config: ProjectConfig,
) -> None:
    mock_time.side_effect = [0.0, 0.1]
    mock_run.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="ok\n",
        stderr="",
    )
    custom = CommandSpec(
        group="maintenance",
        command=["/usr/bin/systemctl", "restart", "caddy.service"],
        timeout=30,
    )
    registry = build_registry({"restart_caddy": custom})
    result = execute_command(
        "restart_caddy", project_config, "req-c1", registry=registry
    )
    assert result.success is True
    assert result.command == "restart_caddy"
    assert result.group == "maintenance"


def test_parameterless_command_rejects_supplied_params() -> None:
    from stormpulse.commands.registry import COMMAND_REGISTRY, execute_command
    from tests.helpers import DUMMY_PROJECT

    with patch('stormpulse.commands.registry.subprocess.run') as run:
        with pytest.raises(ParamValidationError, match='Unknown params'):
            execute_command(
                'git_pull',
                DUMMY_PROJECT,
                'r',
                registry=COMMAND_REGISTRY,
                runtime_params={'extra': 'ignored-before'},
            )
    run.assert_not_called()


# -- docker_logs -------------------------------------------------------------


def test_docker_logs_in_registry() -> None:
    cmd = get_command("docker_logs", registry=COMMAND_REGISTRY)
    assert cmd.group == "diagnostics"
    assert cmd.timeout == 30


def test_docker_logs_resolution(project_config: ProjectConfig) -> None:
    cmd = get_command("docker_logs", registry=COMMAND_REGISTRY)
    validated = validate_params(cmd, {})
    resolved = _resolve_command(cmd.command, project_config, validated)
    assert resolved == [
        "/usr/bin/docker",
        "compose",
        "--env-file",
        "/opt/myapp/.env",
        "-f",
        "/opt/myapp/docker-compose.yml",
        "logs",
        "--tail",
        "100",
        "web",
    ]


@patch("stormpulse.commands.registry.time.monotonic")
@patch("stormpulse.commands.registry.subprocess.run")
def test_execute_docker_logs(
    mock_run: MagicMock,
    mock_time: MagicMock,
    project_config: ProjectConfig,
) -> None:
    mock_time.side_effect = [0.0, 0.2]
    mock_run.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="log line 1\nlog line 2\n",
        stderr="",
    )
    result = execute_command(
        "docker_logs", project_config, "req-logs", registry=COMMAND_REGISTRY
    )
    assert result.success is True
    assert result.command == "docker_logs"
    assert result.group == "diagnostics"
    called_args = mock_run.call_args[0][0]
    assert "--tail" in called_args
    assert "100" in called_args
    assert "web" in called_args
