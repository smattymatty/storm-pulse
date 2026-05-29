"""Sign-off seal enforcement at dispatch time.

The agent re-checks the seal flag whenever a command request arrives,
not just when the registry is built, because the operator can run
``stormpulse signoff seal`` at any time and the next inbound
``run_verify_block`` or ``run_apply_block`` must be refused. Both the
single-command and the sequence dispatch paths consult the same
predicate here so the rule cannot drift between them.

See ADR CORE-004 for the threat model behind the verify and apply
hatches and why the seal is enforced on both registry build and
dispatch.
"""

from __future__ import annotations

from collections.abc import Iterable

from stormpulse.config import CommandDef
from stormpulse.protocol import CommandResultPayload
from stormpulse.signoff import SignoffState

VERIFY_BLOCK_COMMAND = "run_verify_block"
APPLY_BLOCK_COMMAND = "run_apply_block"
SEALED_COMMANDS = frozenset({VERIFY_BLOCK_COMMAND, APPLY_BLOCK_COMMAND})


def is_blocked_by_seal(
    state: SignoffState, commands: Iterable[str],
) -> bool:
    """Return ``True`` when any of *commands* is a sealed hatch AND the agent is sealed."""
    return state.is_sealed() and bool(SEALED_COMMANDS & set(commands))


def sealed_refusal_result(
    request_id: str, command: str, cmd_def: CommandDef | None,
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
