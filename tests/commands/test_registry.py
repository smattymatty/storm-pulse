# skylos: ignore[SKY-Q502] one test module per source module; the registry has this many seams
"""The command registry: its built-ins, lookup, ``build_registry`` and the two
sealed shell hatches it declares.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from stormpulse.commands import (
    COMMAND_REGISTRY,
    CommandError,
    CommandSpec,
    ParamValidationError,
    build_registry,
    get_command,
    validate_params,
)
from stormpulse.commands.registry import _resolve_command
from stormpulse.config import ProjectConfig

# -- registry structure ------------------------------------------------------


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


def test_command_spec_result_is_a_dataclass_on_a_job_only() -> None:
    """``result`` names the shape under ``command.result.result`` (CORE-011 d9):
    a dataclass the typed walker can declare, on the one mode that returns one."""

    @dataclass(frozen=True)
    class Figures:
        deleted_count: int

    job = CommandSpec(
        group="g",
        command=["j"],
        timeout=5,
        mode="job",
        handler=lambda p: None,
        result=Figures,
    )
    assert job.result is Figures
    with pytest.raises(ValueError, match="result must be a dataclass on a 'job'"):
        CommandSpec(group="g", command=["/bin/true"], timeout=5, result=Figures)
    with pytest.raises(ValueError, match="result must be a dataclass on a 'job'"):
        CommandSpec(
            group="g",
            command=["j"],
            timeout=5,
            mode="job",
            handler=lambda p: None,
            result=dict,
        )


# -- lookup ------------------------------------------------------------------


def test_get_command_valid() -> None:
    cmd = get_command("git_pull", registry=COMMAND_REGISTRY)
    assert cmd is COMMAND_REGISTRY["git_pull"]


def test_get_command_invalid_raises() -> None:
    with pytest.raises(CommandError, match="Unknown command"):
        get_command("rm_rf_slash", registry=COMMAND_REGISTRY)


def test_get_command_error_lists_valid_commands() -> None:
    with pytest.raises(CommandError, match="git_pull"):
        get_command("nope", registry=COMMAND_REGISTRY)


# -- build_registry ----------------------------------------------------------


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


# -- run_verify_block: the sign-off checklist's verify hatch -----------------


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


# -- run_apply_block: the sibling hatch, larger cap and longer timeout -------


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
    # Wire contract: apply takes apply_command, verify takes verify_command. A dashboard
    # sending the wrong param name must fail validation rather than silently firing an
    # empty shell.
    cmd = COMMAND_REGISTRY["run_apply_block"]
    with pytest.raises(ParamValidationError, match="Unknown params"):
        validate_params(cmd, {"verify_command": "echo ok"})
