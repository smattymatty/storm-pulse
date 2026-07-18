"""Tests for stormpulse.commands (registry, execution, deploy sequence)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from stormpulse.commands import (
    COMMAND_REGISTRY,
    CommandSpec,
    CommandError,
    ParamValidationError,
    build_registry,
    execute_command,
    get_command,
    non_secret_params,
    run_deploy_sequence,
    validate_params,
)
from stormpulse.commands.registry import _resolve_command
from stormpulse.config import ParamDef, ProjectConfig

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def project_config() -> ProjectConfig:
    return ProjectConfig(
        project_dir=Path("/opt/myapp"),
        compose_file=Path("/opt/myapp/docker-compose.yml"),
        docker_service_name="web",
        env_file=Path("/opt/myapp/.env"),
    )


# ---------------------------------------------------------------------------
# Registry structure
# ---------------------------------------------------------------------------


def test_registry_has_four_commands() -> None:
    assert len(COMMAND_REGISTRY) == 4


def test_all_expected_commands_present() -> None:
    expected = {"git_pull", "docker_logs", "run_verify_block", "run_apply_block"}
    assert set(COMMAND_REGISTRY.keys()) == expected


def test_all_commands_use_absolute_paths() -> None:
    for name, cmd_def in COMMAND_REGISTRY.items():
        assert cmd_def.command[0].startswith("/"), f"{name} binary is not absolute"


def test_all_commands_have_groups() -> None:
    for name, cmd_def in COMMAND_REGISTRY.items():
        assert cmd_def.group, f"{name} has empty group"


def test_builtin_commands_no_confirmation() -> None:
    for name in ("git_pull", "docker_logs"):
        assert COMMAND_REGISTRY[name].requires_confirmation is False, (
            f"{name} should not require confirmation"
        )


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


# ---------------------------------------------------------------------------
# docker_logs
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Deploy sequence
# ---------------------------------------------------------------------------


@patch("stormpulse.commands.deploy.execute_command")
def test_deploy_all_success(
    mock_exec: MagicMock, project_config: ProjectConfig
) -> None:
    mock_exec.side_effect = lambda name, cfg, rid, sid, *, registry: MagicMock(
        success=True,
        command=name,
        sequence_id=sid,
        request_id=rid,
    )
    results = list(
        run_deploy_sequence(
            ["git_pull", "docker_logs"],
            project_config,
            "seq-1",
            registry=COMMAND_REGISTRY,
        )
    )
    assert len(results) == 2
    assert all(r.success for r in results)


@patch("stormpulse.commands.deploy.execute_command")
def test_deploy_stop_on_failure(
    mock_exec: MagicMock, project_config: ProjectConfig
) -> None:
    def side_effect(
        name: str, cfg: Any, rid: str, sid: str | None, *, registry: Any
    ) -> MagicMock:
        return MagicMock(success=(name != "docker_logs"), command=name)

    mock_exec.side_effect = side_effect
    results = list(
        run_deploy_sequence(
            ["git_pull", "docker_logs"],
            project_config,
            "seq-2",
            registry=COMMAND_REGISTRY,
        )
    )
    assert len(results) == 2  # git_pull (ok), docker_logs (fail), then stop
    assert results[0].success is True
    assert results[1].success is False


@patch("stormpulse.commands.deploy.execute_command")
def test_deploy_continue_on_failure(
    mock_exec: MagicMock, project_config: ProjectConfig
) -> None:
    def side_effect(
        name: str, cfg: Any, rid: str, sid: str | None, *, registry: Any
    ) -> MagicMock:
        return MagicMock(success=(name != "docker_logs"), command=name)

    mock_exec.side_effect = side_effect
    results = list(
        run_deploy_sequence(
            ["git_pull", "docker_logs"],
            project_config,
            "seq-3",
            stop_on_failure=False,
            registry=COMMAND_REGISTRY,
        )
    )
    assert len(results) == 2


def test_deploy_invalid_command_raises_upfront(project_config: ProjectConfig) -> None:
    with pytest.raises(CommandError, match="Unknown command"):
        list(
            run_deploy_sequence(
                ["git_pull", "bogus"],
                project_config,
                "seq-4",
                registry=COMMAND_REGISTRY,
            )
        )


@patch("stormpulse.commands.deploy.execute_command")
def test_deploy_unique_request_ids(
    mock_exec: MagicMock, project_config: ProjectConfig
) -> None:
    call_args: list[str] = []

    def side_effect(
        name: str, cfg: Any, rid: str, sid: str | None, *, registry: Any
    ) -> MagicMock:
        call_args.append(rid)
        return MagicMock(success=True, command=name)

    mock_exec.side_effect = side_effect
    list(
        run_deploy_sequence(
            ["git_pull", "docker_logs"],
            project_config,
            "seq-5",
            registry=COMMAND_REGISTRY,
        )
    )
    assert len(set(call_args)) == 2  # all unique


@patch("stormpulse.commands.deploy.execute_command")
def test_deploy_shared_sequence_id(
    mock_exec: MagicMock, project_config: ProjectConfig
) -> None:
    call_sids: list[str | None] = []

    def side_effect(
        name: str, cfg: Any, rid: str, sid: str | None, *, registry: Any
    ) -> MagicMock:
        call_sids.append(sid)
        return MagicMock(success=True, command=name)

    mock_exec.side_effect = side_effect
    list(
        run_deploy_sequence(
            ["git_pull", "docker_logs"],
            project_config,
            "seq-6",
            registry=COMMAND_REGISTRY,
        )
    )
    assert all(sid == "seq-6" for sid in call_sids)


# ---------------------------------------------------------------------------
# build_registry
# ---------------------------------------------------------------------------


def test_build_registry_no_config_commands() -> None:
    registry = build_registry({})
    assert registry == COMMAND_REGISTRY


def test_build_registry_adds_custom_command() -> None:
    custom = CommandSpec(
        group="maintenance",
        command=["/usr/bin/systemctl", "restart", "caddy.service"],
        timeout=30,
        description="Restart Caddy",
    )
    registry = build_registry({"restart_caddy": custom})
    # 4 built-ins (git_pull, docker_logs, run_verify_block, run_apply_block) + 1 custom.
    assert len(registry) == 5
    assert registry["restart_caddy"] is custom
    assert registry["git_pull"] is COMMAND_REGISTRY["git_pull"]


def test_build_registry_overrides_builtin() -> None:
    custom_git = CommandSpec(
        group="deploy",
        command=["/usr/local/bin/git", "-C", "{project_dir}", "pull"],
        timeout=120,
    )
    registry = build_registry({"git_pull": custom_git})
    # Override collapses git_pull into one entry; the other three built-ins stay.
    assert len(registry) == 4
    assert registry["git_pull"] is custom_git
    assert registry["git_pull"].timeout == 120


def test_build_registry_disables_builtin() -> None:
    registry = build_registry({}, disabled=frozenset({"docker_logs"}))
    assert "docker_logs" not in registry
    # git_pull + run_verify_block + run_apply_block remain.
    assert len(registry) == 3


def test_build_registry_disables_custom_command() -> None:
    custom = CommandSpec(
        group="maintenance",
        command=["/usr/bin/systemctl", "restart", "caddy.service"],
        timeout=30,
    )
    registry = build_registry(
        {"restart_caddy": custom}, disabled=frozenset({"restart_caddy"})
    )
    assert "restart_caddy" not in registry
    # Four built-ins, custom is disabled away.
    assert len(registry) == 4


def test_build_registry_disables_multiple() -> None:
    registry = build_registry(
        {},
        disabled=frozenset(
            {
                "git_pull",
                "docker_logs",
                "run_verify_block",
                "run_apply_block",
            }
        ),
    )
    assert "git_pull" not in registry
    assert "docker_logs" not in registry
    assert "run_verify_block" not in registry
    assert "run_apply_block" not in registry
    assert len(registry) == 0


def test_build_registry_disabled_unknown_is_harmless() -> None:
    registry = build_registry({}, disabled=frozenset({"nonexistent_command"}))
    assert len(registry) == 4


def test_build_registry_does_not_mutate_original() -> None:
    custom = CommandSpec(
        group="test",
        command=["/bin/true"],
        timeout=10,
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


# ---------------------------------------------------------------------------
# validate_params
# ---------------------------------------------------------------------------


def _cmd_with_params(**params: ParamDef) -> CommandSpec:
    """Helper to build a CommandSpec with params."""
    return CommandSpec(
        group="test",
        command=["/bin/true"],
        timeout=10,
        params=params,
    )


def test_validate_params_no_params_defined() -> None:
    cmd = _cmd_with_params()
    assert validate_params(cmd, {}) == {}


def test_validate_params_uses_defaults() -> None:
    cmd = _cmd_with_params(
        service=ParamDef(placeholder="service", default="web", pattern="[a-z]+"),
    )
    assert validate_params(cmd, {}) == {"service": "web"}


def test_validate_params_overrides_default() -> None:
    cmd = _cmd_with_params(
        service=ParamDef(placeholder="service", default="web", pattern="[a-z]+"),
    )
    assert validate_params(cmd, {"service": "celery"}) == {"service": "celery"}


def test_validate_params_unknown_param_raises() -> None:
    cmd = _cmd_with_params(
        service=ParamDef(placeholder="service", default="web", pattern="[a-z]+"),
    )
    with pytest.raises(ParamValidationError, match="Unknown"):
        validate_params(cmd, {"bogus": "value"})


def test_validate_params_pattern_mismatch_raises() -> None:
    cmd = _cmd_with_params(
        service=ParamDef(placeholder="service", default="web", pattern="[a-z]+"),
    )
    with pytest.raises(ParamValidationError, match="pattern"):
        validate_params(cmd, {"service": "INVALID!!!"})


def test_validate_params_secret_pattern_mismatch_withholds_value() -> None:
    """A failing secret value never rides the error message (it lands in logs)."""
    cmd = _cmd_with_params(
        secret_access_key=ParamDef(
            placeholder="secret_access_key", default=None, pattern=r".+", secret=True,
        ),
    )
    leaked = "SuperSecretValue123\n"
    with pytest.raises(ParamValidationError, match="withheld") as excinfo:
        validate_params(cmd, {"secret_access_key": leaked})
    assert "SuperSecretValue123" not in str(excinfo.value)


def test_validate_params_none_default_no_override_skips() -> None:
    """When default is None and no runtime override, the param is skipped.

    The config-level placeholder (e.g. docker_service_name) provides the
    fallback value during _resolve_command.
    """
    cmd = _cmd_with_params(
        service=ParamDef(placeholder="service", default=None, pattern="[a-z]+"),
    )
    assert validate_params(cmd, {}) == {}


def test_validate_params_none_default_with_override() -> None:
    cmd = _cmd_with_params(
        service=ParamDef(placeholder="service", default=None, pattern="[a-z]+"),
    )
    assert validate_params(cmd, {"service": "celery"}) == {"service": "celery"}


# ---------------------------------------------------------------------------
# Resolution with param overrides
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# run_verify_block - dashboard-driven verify-block execution for the
# sign-off checklist feature in the Storm Developments website.
# ---------------------------------------------------------------------------


def test_run_verify_block_registered() -> None:
    assert "run_verify_block" in COMMAND_REGISTRY
    cmd = COMMAND_REGISTRY["run_verify_block"]
    assert cmd.group == "signoff"
    assert cmd.command == ["/bin/bash", "-c", "{verify_command}"]


def test_run_verify_block_param_has_byte_cap() -> None:
    cmd = COMMAND_REGISTRY["run_verify_block"]
    pdef = cmd.params["verify_command"]
    # Opaque shell text: no regex pattern, just a size cap.
    assert pdef.pattern is None
    assert pdef.max_bytes is not None
    assert pdef.max_bytes >= 1024  # at least enough for typical verify commands


def test_run_verify_block_accepts_simple_shell(project_config: ProjectConfig) -> None:
    cmd = COMMAND_REGISTRY["run_verify_block"]
    validated = validate_params(cmd, {"verify_command": "sudo ufw status | head -1"})
    assert validated == {"verify_command": "sudo ufw status | head -1"}
    resolved = _resolve_command(cmd.command, project_config, validated)
    assert resolved == ["/bin/bash", "-c", "sudo ufw status | head -1"]


def test_run_verify_block_accepts_shell_with_braces(
    project_config: ProjectConfig,
) -> None:
    # Shell parameter expansion uses ${...} - format_map must not
    # interpret those as Python format placeholders.
    cmd = COMMAND_REGISTRY["run_verify_block"]
    validated = validate_params(cmd, {"verify_command": "echo ${HOME}"})
    resolved = _resolve_command(cmd.command, project_config, validated)
    assert resolved == ["/bin/bash", "-c", "echo ${HOME}"]


def test_run_verify_block_rejects_oversized_payload() -> None:
    cmd = COMMAND_REGISTRY["run_verify_block"]
    pdef = cmd.params["verify_command"]
    assert pdef.max_bytes is not None
    too_big = "x" * (pdef.max_bytes + 1)
    with pytest.raises(ParamValidationError, match="exceeds max_bytes"):
        validate_params(cmd, {"verify_command": too_big})


def test_run_verify_block_rejects_unknown_param() -> None:
    cmd = COMMAND_REGISTRY["run_verify_block"]
    with pytest.raises(ParamValidationError, match="Unknown params"):
        validate_params(cmd, {"verify_command": "echo ok", "evil": "rm -rf /"})


# ---------------------------------------------------------------------------
# run_apply_block - dashboard-driven apply-block execution. Sibling of
# run_verify_block; larger byte cap and longer timeout because apply
# scripts include image pulls, vulnerability scans, and multi-line
# heredocs.
# ---------------------------------------------------------------------------


def test_run_apply_block_registered() -> None:
    assert "run_apply_block" in COMMAND_REGISTRY
    cmd = COMMAND_REGISTRY["run_apply_block"]
    assert cmd.group == "signoff"
    assert cmd.command == ["/bin/bash", "-c", "{apply_command}"]


def test_run_apply_block_timeout_is_longer_than_verify() -> None:
    apply_cmd = COMMAND_REGISTRY["run_apply_block"]
    verify_cmd = COMMAND_REGISTRY["run_verify_block"]
    assert apply_cmd.timeout > verify_cmd.timeout
    # Has to cover docker pull + grype scan; 600s is the chosen budget.
    assert apply_cmd.timeout >= 300


def test_run_apply_block_param_has_larger_byte_cap_than_verify() -> None:
    apply_pdef = COMMAND_REGISTRY["run_apply_block"].params["apply_command"]
    verify_pdef = COMMAND_REGISTRY["run_verify_block"].params["verify_command"]
    assert apply_pdef.pattern is None
    assert apply_pdef.max_bytes is not None
    assert verify_pdef.max_bytes is not None
    assert apply_pdef.max_bytes > verify_pdef.max_bytes
    # Sized for a Compose-file heredoc plus surrounding shell.
    assert apply_pdef.max_bytes >= 8192


def test_run_apply_block_accepts_multiline_heredoc(
    project_config: ProjectConfig,
) -> None:
    cmd = COMMAND_REGISTRY["run_apply_block"]
    script = (
        "cat > /tmp/compose.yml <<'EOF'\n"
        "services:\n"
        "  web:\n"
        "    image: placeholder:latest\n"
        "EOF\n"
        "docker compose -f /tmp/compose.yml config"
    )
    validated = validate_params(cmd, {"apply_command": script})
    assert validated == {"apply_command": script}
    resolved = _resolve_command(cmd.command, project_config, validated)
    assert resolved == ["/bin/bash", "-c", script]


def test_run_apply_block_rejects_oversized_payload() -> None:
    cmd = COMMAND_REGISTRY["run_apply_block"]
    pdef = cmd.params["apply_command"]
    assert pdef.max_bytes is not None
    too_big = "x" * (pdef.max_bytes + 1)
    with pytest.raises(ParamValidationError, match="exceeds max_bytes"):
        validate_params(cmd, {"apply_command": too_big})


def test_run_apply_block_rejects_unknown_param() -> None:
    cmd = COMMAND_REGISTRY["run_apply_block"]
    with pytest.raises(ParamValidationError, match="Unknown params"):
        validate_params(cmd, {"apply_command": "echo ok", "evil": "rm -rf /"})


def test_run_apply_block_rejects_verify_param_name() -> None:
    # Wire contract distinction: apply uses apply_command, verify uses
    # verify_command. A dashboard that sends the wrong param name for
    # the dispatched command must fail validation rather than silently
    # firing an empty shell.
    cmd = COMMAND_REGISTRY["run_apply_block"]
    with pytest.raises(ParamValidationError, match="Unknown params"):
        validate_params(cmd, {"verify_command": "echo ok"})


def test_non_secret_params_drops_secret_flagged() -> None:
    # A secret param reaches the handler but must never ride event/log context.
    cmd = CommandSpec(
        group="rclone",
        command=["/rclone"],
        timeout=60,
        mode="job",
        handler=lambda p: None,
        params={
            "bucket_id": ParamDef("bucket_id", default=None, pattern=r".+"),
            "secret_access_key": ParamDef(
                "secret_access_key", default=None, pattern=r".+", secret=True
            ),
        },
    )
    full = {"bucket_id": "abc", "secret_access_key": "wJalrXUtnFEMI"}
    assert non_secret_params(cmd, full) == {"bucket_id": "abc"}


def test_non_secret_params_passes_all_when_none_secret() -> None:
    cmd = CommandSpec(
        group="deploy", command=["/bin/git", "pull"], timeout=60,
        params={"branch": ParamDef("branch", default=None, pattern=r".+")},
    )
    assert non_secret_params(cmd, {"branch": "main"}) == {"branch": "main"}


def test_credential_shaped_param_name_requires_secret_flag() -> None:
    # Fix-the-system guard for the events-plane leak: a future param named
    # like a credential cannot be constructed untagged.
    with pytest.raises(ValueError, match="secret=True"):
        ParamDef("api_token", default=None, pattern=r".+")
    with pytest.raises(ValueError, match="secret=True"):
        ParamDef("db_password", default=None, pattern=r".+")
    # Tagged, it constructs fine.
    pdef = ParamDef("api_token", default=None, pattern=r".+", secret=True)
    assert pdef.secret is True
