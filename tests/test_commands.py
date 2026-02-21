"""Tests for stormpulse.commands (registry, execution, deploy sequence)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from stormpulse.commands import (
    COMMAND_REGISTRY,
    DEFAULT_DEPLOY_SEQUENCE,
    CommandDef,
    CommandError,
    build_registry,
    execute_command,
    get_command,
    run_deploy_sequence,
)
from stormpulse.commands.registry import _resolve_command
from stormpulse.config import ProjectConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_config() -> ProjectConfig:
    return ProjectConfig(
        project_dir=Path("/opt/myapp"),
        compose_file=Path("/opt/myapp/docker-compose.yml"),
        docker_service_name="web",
    )


# ---------------------------------------------------------------------------
# Registry structure
# ---------------------------------------------------------------------------


def test_registry_has_five_commands() -> None:
    assert len(COMMAND_REGISTRY) == 5


def test_all_expected_commands_present() -> None:
    expected = {"git_pull", "docker_build", "docker_down", "docker_up", "django_migrate"}
    assert set(COMMAND_REGISTRY.keys()) == expected


def test_all_commands_use_absolute_paths() -> None:
    for name, cmd_def in COMMAND_REGISTRY.items():
        assert cmd_def.command[0].startswith("/"), f"{name} binary is not absolute"


def test_all_commands_have_groups() -> None:
    for name, cmd_def in COMMAND_REGISTRY.items():
        assert cmd_def.group, f"{name} has empty group"


def test_docker_down_requires_confirmation() -> None:
    assert COMMAND_REGISTRY["docker_down"].requires_confirmation is True


def test_non_destructive_commands_no_confirmation() -> None:
    for name in ("git_pull", "docker_build", "docker_up", "django_migrate"):
        assert COMMAND_REGISTRY[name].requires_confirmation is False, f"{name} should not require confirmation"


def test_command_def_is_frozen() -> None:
    cmd = COMMAND_REGISTRY["git_pull"]
    with pytest.raises(AttributeError):
        cmd.group = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def test_get_command_valid() -> None:
    cmd = get_command("git_pull", registry=COMMAND_REGISTRY)
    assert cmd is COMMAND_REGISTRY["git_pull"]


def test_get_command_invalid_raises() -> None:
    with pytest.raises(CommandError, match="Unknown command"):
        get_command("rm_rf_slash", registry=COMMAND_REGISTRY)


def test_get_command_error_lists_valid_commands() -> None:
    with pytest.raises(CommandError, match="git_pull"):
        get_command("nope", registry=COMMAND_REGISTRY)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_resolve_project_dir(project_config: ProjectConfig) -> None:
    template = ["/usr/bin/git", "-C", "{project_dir}", "pull"]
    resolved = _resolve_command(template, project_config)
    assert resolved == ["/usr/bin/git", "-C", "/opt/myapp", "pull"]


def test_resolve_compose_file(project_config: ProjectConfig) -> None:
    template = ["/usr/bin/docker", "compose", "-f", "{compose_file}", "build"]
    resolved = _resolve_command(template, project_config)
    assert resolved[3] == "/opt/myapp/docker-compose.yml"


def test_resolve_docker_service_name(project_config: ProjectConfig) -> None:
    template = ["/usr/bin/docker", "compose", "-f", "{compose_file}", "exec", "{docker_service_name}", "python"]
    resolved = _resolve_command(template, project_config)
    assert resolved[5] == "web"


def test_resolve_unknown_placeholder_raises(project_config: ProjectConfig) -> None:
    template = ["/usr/bin/git", "-C", "{unknown_dir}", "pull"]
    with pytest.raises(ValueError, match="Unknown placeholder"):
        _resolve_command(template, project_config)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@patch("stormpulse.commands.registry.time.monotonic")
@patch("stormpulse.commands.registry.subprocess.run")
def test_execute_success(
    mock_run: MagicMock,
    mock_time: MagicMock,
    project_config: ProjectConfig,
) -> None:
    mock_time.side_effect = [0.0, 0.342]
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="Already up to date.\n", stderr="",
    )
    result = execute_command("git_pull", project_config, "req-1", registry=COMMAND_REGISTRY)
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
        args=[], returncode=1, stdout="", stderr="error: permission denied\n",
    )
    result = execute_command("git_pull", project_config, "req-2", registry=COMMAND_REGISTRY)
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
    result = execute_command("git_pull", project_config, "req-3", registry=COMMAND_REGISTRY)
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
    mock_run.side_effect = FileNotFoundError("[Errno 2] No such file or directory: '/usr/bin/git'")
    result = execute_command("git_pull", project_config, "req-4", registry=COMMAND_REGISTRY)
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
    result = execute_command("git_pull", project_config, "req-5", registry=COMMAND_REGISTRY)
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
    mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    result = execute_command("docker_up", project_config, "req-6", registry=COMMAND_REGISTRY)
    assert result.duration_ms == 500


@patch("stormpulse.commands.registry.time.monotonic")
@patch("stormpulse.commands.registry.subprocess.run")
def test_execute_forwards_sequence_id(
    mock_run: MagicMock,
    mock_time: MagicMock,
    project_config: ProjectConfig,
) -> None:
    mock_time.side_effect = [0.0, 0.1]
    mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    result = execute_command(
        "git_pull", project_config, "req-7", sequence_id="seq-42", registry=COMMAND_REGISTRY,
    )
    assert result.sequence_id == "seq-42"


def test_execute_unknown_command_raises(project_config: ProjectConfig) -> None:
    with pytest.raises(CommandError, match="Unknown command"):
        execute_command("nope", project_config, "req-x", registry=COMMAND_REGISTRY)


# ---------------------------------------------------------------------------
# Deploy sequence
# ---------------------------------------------------------------------------


@patch("stormpulse.commands.deploy.execute_command")
def test_deploy_all_success(mock_exec: MagicMock, project_config: ProjectConfig) -> None:
    mock_exec.side_effect = lambda name, cfg, rid, sid, *, registry: MagicMock(
        success=True, command=name, sequence_id=sid, request_id=rid,
    )
    results = list(run_deploy_sequence(
        DEFAULT_DEPLOY_SEQUENCE, project_config, "seq-1", registry=COMMAND_REGISTRY,
    ))
    assert len(results) == 5
    assert all(r.success for r in results)


@patch("stormpulse.commands.deploy.execute_command")
def test_deploy_stop_on_failure(mock_exec: MagicMock, project_config: ProjectConfig) -> None:
    def side_effect(name: str, cfg: Any, rid: str, sid: str | None, *, registry: Any) -> MagicMock:
        return MagicMock(success=(name != "docker_build"), command=name)

    mock_exec.side_effect = side_effect
    results = list(run_deploy_sequence(
        DEFAULT_DEPLOY_SEQUENCE, project_config, "seq-2", registry=COMMAND_REGISTRY,
    ))
    assert len(results) == 2  # git_pull (ok), docker_build (fail), then stop
    assert results[0].success is True
    assert results[1].success is False


@patch("stormpulse.commands.deploy.execute_command")
def test_deploy_continue_on_failure(mock_exec: MagicMock, project_config: ProjectConfig) -> None:
    def side_effect(name: str, cfg: Any, rid: str, sid: str | None, *, registry: Any) -> MagicMock:
        return MagicMock(success=(name != "docker_build"), command=name)

    mock_exec.side_effect = side_effect
    results = list(run_deploy_sequence(
        DEFAULT_DEPLOY_SEQUENCE, project_config, "seq-3",
        stop_on_failure=False, registry=COMMAND_REGISTRY,
    ))
    assert len(results) == 5


def test_deploy_invalid_command_raises_upfront(project_config: ProjectConfig) -> None:
    with pytest.raises(CommandError, match="Unknown command"):
        list(run_deploy_sequence(
            ["git_pull", "bogus"], project_config, "seq-4", registry=COMMAND_REGISTRY,
        ))


@patch("stormpulse.commands.deploy.execute_command")
def test_deploy_unique_request_ids(mock_exec: MagicMock, project_config: ProjectConfig) -> None:
    call_args: list[str] = []

    def side_effect(name: str, cfg: Any, rid: str, sid: str | None, *, registry: Any) -> MagicMock:
        call_args.append(rid)
        return MagicMock(success=True, command=name)

    mock_exec.side_effect = side_effect
    list(run_deploy_sequence(
        DEFAULT_DEPLOY_SEQUENCE, project_config, "seq-5", registry=COMMAND_REGISTRY,
    ))
    assert len(set(call_args)) == 5  # all unique


@patch("stormpulse.commands.deploy.execute_command")
def test_deploy_shared_sequence_id(mock_exec: MagicMock, project_config: ProjectConfig) -> None:
    call_sids: list[str | None] = []

    def side_effect(name: str, cfg: Any, rid: str, sid: str | None, *, registry: Any) -> MagicMock:
        call_sids.append(sid)
        return MagicMock(success=True, command=name)

    mock_exec.side_effect = side_effect
    list(run_deploy_sequence(
        DEFAULT_DEPLOY_SEQUENCE, project_config, "seq-6", registry=COMMAND_REGISTRY,
    ))
    assert all(sid == "seq-6" for sid in call_sids)


# ---------------------------------------------------------------------------
# build_registry
# ---------------------------------------------------------------------------


def test_build_registry_no_config_commands() -> None:
    registry = build_registry({})
    assert registry == COMMAND_REGISTRY


def test_build_registry_adds_custom_command() -> None:
    custom = CommandDef(
        group="maintenance",
        command=["/usr/bin/systemctl", "restart", "caddy.service"],
        timeout=30,
        description="Restart Caddy",
    )
    registry = build_registry({"restart_caddy": custom})
    assert len(registry) == 6
    assert registry["restart_caddy"] is custom
    assert registry["git_pull"] is COMMAND_REGISTRY["git_pull"]


def test_build_registry_overrides_builtin() -> None:
    custom_git = CommandDef(
        group="deploy",
        command=["/usr/local/bin/git", "-C", "{project_dir}", "pull"],
        timeout=120,
    )
    registry = build_registry({"git_pull": custom_git})
    assert len(registry) == 5
    assert registry["git_pull"] is custom_git
    assert registry["git_pull"].timeout == 120


def test_build_registry_does_not_mutate_original() -> None:
    custom = CommandDef(
        group="test", command=["/bin/true"], timeout=10,
    )
    registry = build_registry({"test_cmd": custom})
    assert "test_cmd" in registry
    assert "test_cmd" not in COMMAND_REGISTRY


@patch("stormpulse.commands.registry.time.monotonic")
@patch("stormpulse.commands.registry.subprocess.run")
def test_execute_config_defined_command(
    mock_run: MagicMock,
    mock_time: MagicMock,
    project_config: ProjectConfig,
) -> None:
    mock_time.side_effect = [0.0, 0.1]
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="ok\n", stderr="",
    )
    custom = CommandDef(
        group="maintenance",
        command=["/usr/bin/systemctl", "restart", "caddy.service"],
        timeout=30,
    )
    registry = build_registry({"restart_caddy": custom})
    result = execute_command("restart_caddy", project_config, "req-c1", registry=registry)
    assert result.success is True
    assert result.command == "restart_caddy"
    assert result.group == "maintenance"
