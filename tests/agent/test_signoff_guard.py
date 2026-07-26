"""Tests for the sign-off seal predicate and refusal builder."""

from __future__ import annotations

from pathlib import Path

from stormpulse.agent.signoff_guard import (
    APPLY_BLOCK_COMMAND,
    SEALED_COMMANDS,
    VERIFY_BLOCK_COMMAND,
    is_blocked_by_seal,
    needs_restart_to_load,
    restart_pending_result,
    sealed_refusal_result,
)
from stormpulse.config import CommandSpec
from stormpulse.signoff import SignoffState


def _hatch_spec() -> CommandSpec:
    return CommandSpec(
        group="signoff",
        command=["/bin/bash", "-c", "{verify_command}"],
        timeout=30,
    )


def test_sealed_commands_set_covers_verify_and_apply() -> None:
    assert SEALED_COMMANDS == {VERIFY_BLOCK_COMMAND, APPLY_BLOCK_COMMAND}


def test_unsealed_state_blocks_nothing(tmp_path: Path) -> None:
    state = SignoffState(tmp_path)
    assert not is_blocked_by_seal(state, [VERIFY_BLOCK_COMMAND])
    assert not is_blocked_by_seal(state, [APPLY_BLOCK_COMMAND])
    assert not is_blocked_by_seal(state, ["git_pull", "docker_logs"])


def test_sealed_state_blocks_only_seal_gated_commands(tmp_path: Path) -> None:
    state = SignoffState(tmp_path)
    state.seal()
    assert is_blocked_by_seal(state, [VERIFY_BLOCK_COMMAND])
    assert is_blocked_by_seal(state, [APPLY_BLOCK_COMMAND])
    assert is_blocked_by_seal(state, ["git_pull", VERIFY_BLOCK_COMMAND])
    assert is_blocked_by_seal(state, ["git_pull", APPLY_BLOCK_COMMAND])
    assert is_blocked_by_seal(
        state,
        [VERIFY_BLOCK_COMMAND, APPLY_BLOCK_COMMAND],
    )
    assert not is_blocked_by_seal(state, ["git_pull", "docker_logs"])


def test_sealed_refusal_includes_cmd_def_group(tmp_path: Path) -> None:
    cmd_def = CommandSpec(
        group="signoff",
        command=["/bin/bash", "-c", "{verify_command}"],
        timeout=30,
    )
    result = sealed_refusal_result("req-1", VERIFY_BLOCK_COMMAND, cmd_def)
    assert result.success is False
    assert result.failure_reason == "signoff_sealed"
    assert result.group == "signoff"
    assert result.request_id == "req-1"
    assert "stormpulse signoff unseal" in result.stderr


def test_sealed_refusal_for_apply_command_carries_command_name() -> None:
    cmd_def = CommandSpec(
        group="signoff",
        command=["/bin/bash", "-c", "{apply_command}"],
        timeout=600,
    )
    result = sealed_refusal_result("req-apply-1", APPLY_BLOCK_COMMAND, cmd_def)
    assert result.command == APPLY_BLOCK_COMMAND
    assert result.failure_reason == "signoff_sealed"
    assert result.exit_code == -1
    assert result.success is False


def test_sealed_refusal_falls_back_to_signoff_group_when_no_cmd_def() -> None:
    result = sealed_refusal_result("req-2", VERIFY_BLOCK_COMMAND, None)
    assert result.group == "signoff"
    assert result.failure_reason == "signoff_sealed"


# ---------------------------------------------------------------------------
# needs_restart_to_load: the unseal-before-restart window (ADR CORE-004)
# ---------------------------------------------------------------------------


def test_needs_restart_when_unsealed_but_hatch_command_absent(tmp_path: Path) -> None:
    """Unsealed agent, hatch command missing from the boot-built registry."""
    state = SignoffState(tmp_path)  # unsealed
    registry: dict[str, CommandSpec] = {"git_pull": _hatch_spec()}
    assert needs_restart_to_load(state, registry, [VERIFY_BLOCK_COMMAND]) == (
        VERIFY_BLOCK_COMMAND
    )
    assert needs_restart_to_load(state, registry, [APPLY_BLOCK_COMMAND]) == (
        APPLY_BLOCK_COMMAND
    )


def test_no_restart_when_hatch_command_already_loaded(tmp_path: Path) -> None:
    """Booted unsealed: the command is in the registry, nothing to reload."""
    state = SignoffState(tmp_path)
    registry = {VERIFY_BLOCK_COMMAND: _hatch_spec()}
    assert needs_restart_to_load(state, registry, [VERIFY_BLOCK_COMMAND]) is None


def test_no_restart_when_sealed(tmp_path: Path) -> None:
    """Sealed is layer 2's job; a sealed absent command is not a restart case."""
    state = SignoffState(tmp_path)
    state.seal()
    registry: dict[str, CommandSpec] = {}
    assert needs_restart_to_load(state, registry, [VERIFY_BLOCK_COMMAND]) is None


def test_genuinely_unknown_command_is_not_deflected(tmp_path: Path) -> None:
    """A non-hatch unknown command must still alarm, never get a restart pass."""
    state = SignoffState(tmp_path)
    registry: dict[str, CommandSpec] = {}
    assert needs_restart_to_load(state, registry, ["rm_rf_slash"]) is None


def test_needs_restart_returns_first_offending_command_in_sequence(
    tmp_path: Path,
) -> None:
    state = SignoffState(tmp_path)
    registry: dict[str, CommandSpec] = {"git_pull": _hatch_spec()}
    got = needs_restart_to_load(
        state, registry, ["git_pull", APPLY_BLOCK_COMMAND, VERIFY_BLOCK_COMMAND]
    )
    assert got == APPLY_BLOCK_COMMAND


def test_restart_pending_result_shape() -> None:
    result = restart_pending_result("req-restart-1", VERIFY_BLOCK_COMMAND)
    assert result.command == VERIFY_BLOCK_COMMAND
    assert result.failure_reason == "restart_required"
    assert result.success is False
    assert result.exit_code == -1
    assert result.group == "signoff"
    assert "restart" in result.stderr.lower()
    assert "not a whitelist violation" in result.stderr
