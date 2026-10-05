"""Function 13: every shell hatch ships sealed.

A command whose argv hands text to ``sh -c`` runs whatever the signed envelope
carries, and Function 3's ``shell=True`` scan cannot see it. Every such command
must be in ``SEALED_COMMANDS``, which the seal excludes (CORE-004), and every
sealed name must still be a registered command.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from stormpulse.agent.wire_contract import all_command_specs
from stormpulse.commands.registry import SEALED_COMMANDS
from stormpulse.config import CommandSpec

SHELLS = frozenset({"sh", "bash", "dash", "zsh"})


def check_shell_hatches_sealed(
    specs: dict[str, CommandSpec] | None = None,
    sealed: frozenset[str] = SEALED_COMMANDS,
) -> list[str]:
    """Return violation strings; empty list means clean."""
    specs = all_command_specs() if specs is None else specs
    violations = [
        f"{name}: argv runs {spec.command[0]} -c but is not in SEALED_COMMANDS"
        for name, spec in sorted(specs.items())
        if _runs_shell_text(spec) and name not in sealed
    ]
    violations.extend(
        f"{name}: in SEALED_COMMANDS but not a registered command"
        for name in sorted(sealed - specs.keys())
    )
    return violations


def _runs_shell_text(spec: CommandSpec) -> bool:
    argv = spec.command
    return bool(argv) and PurePosixPath(argv[0]).name in SHELLS and "-c" in argv[1:]
