"""Dispatch-time seal recheck for verify/apply blocks (ADR CORE-004). Shared by single-command and sequence paths."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from stormpulse.config import CommandSpec
from stormpulse.protocol import CommandResultPayload
from stormpulse.signoff import SignoffState

VERIFY_BLOCK_COMMAND = "run_verify_block"
APPLY_BLOCK_COMMAND = "run_apply_block"
SEALED_COMMANDS = frozenset({VERIFY_BLOCK_COMMAND, APPLY_BLOCK_COMMAND})


def is_blocked_by_seal(
    state: SignoffState,
    commands: Iterable[str],
) -> bool:
    """Return ``True`` when any of *commands* is a sealed hatch AND the agent is sealed."""
    return state.is_sealed() and bool(SEALED_COMMANDS & set(commands))


def needs_restart_to_load(
    state: SignoffState,
    registry: Mapping[str, CommandSpec],
    commands: Iterable[str],
) -> str | None:
    """Return the first hatch command that is unsealed but not yet loaded, else ``None``.

    The two seal layers are asymmetric (ADR CORE-004). The registry
    (layer 1) is built once at bootstrap from the boot-time seal state and
    governs admission; the dispatch recheck (layer 2, ``is_blocked_by_seal``)
    reads state live but can only refuse, never re-admit. So unsealing a
    running agent flips the recheck to "not blocked" without putting the
    command back in the registry: it stays absent until a restart re-runs
    ``build_registry``. This detects exactly that window - the agent is
    unsealed, yet a hatch command the operator now expects to run is missing
    from the registry - so the caller can ask for a restart instead of
    firing the whitelist-violation alarm on the operator's own command.
    """
    if state.is_sealed():
        return None
    for name in commands:
        if name in SEALED_COMMANDS and name not in registry:
            return name
    return None


def sealed_refusal_result(
    request_id: str,
    command: str,
    cmd_def: CommandSpec | None,
) -> CommandResultPayload:
    """Build the structured failure the agent emits when refusing a sealed verify or apply block."""
    return CommandResultPayload(
        request_id=request_id,
        command=command,
        group=cmd_def.group if cmd_def else "signoff",
        success=False,
        exit_code=-1,
        stdout="",
        stderr=(
            "Sign-off is sealed on this agent. "
            "Unseal on the host (`stormpulse signoff unseal`) "
            "to re-enable verify-block and apply-block dispatch."
        ),
        duration_ms=0,
        failure_reason="signoff_sealed",
    )


def restart_pending_result(
    request_id: str,
    command: str,
) -> CommandResultPayload:
    """Build the failure emitted when a now-unsealed hatch command is not yet loaded.

    Distinct from ``sealed_refusal_result`` (the agent is *unsealed* here) and
    from a whitelist rejection (the command is legitimate, just not yet in the
    boot-built registry). ``failure_reason="restart_required"`` keeps it out of
    the whitelist-violation security signal.
    """
    return CommandResultPayload(
        request_id=request_id,
        command=command,
        group="signoff",
        success=False,
        exit_code=-1,
        stdout="",
        stderr=(
            "Sign-off was unsealed after this agent started, so verify-block "
            "and apply-block dispatch are not loaded yet: the command whitelist "
            "is built once when the agent boots. Restart the agent to load this "
            "command (rootless: systemctl --user restart stormpulse.service). "
            "This is a pending restart, not a whitelist violation."
        ),
        duration_ms=0,
        failure_reason="restart_required",
    )
