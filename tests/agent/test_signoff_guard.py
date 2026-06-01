"""Tests for the sign-off seal predicate and refusal builder."""

from __future__ import annotations

from pathlib import Path

from stormpulse.agent.signoff_guard import (
    APPLY_BLOCK_COMMAND,
    SEALED_COMMANDS,
    VERIFY_BLOCK_COMMAND,
    is_blocked_by_seal,
    sealed_refusal_result,
)
from stormpulse.config import CommandDef
from stormpulse.signoff import SignoffState


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
    cmd_def = CommandDef(
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
    cmd_def = CommandDef(
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
