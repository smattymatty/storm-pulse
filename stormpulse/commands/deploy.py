"""Deploy sequence runner — executes a series of whitelisted commands."""

from __future__ import annotations

import uuid
from collections.abc import Generator

from stormpulse.config import ProjectConfig
from stormpulse.protocol import CommandResultPayload

from .registry import execute_command, get_command

DEFAULT_DEPLOY_SEQUENCE: list[str] = [
    "git_pull",
    "docker_build",
    "docker_down",
    "docker_up",
    "django_migrate",
]


def run_deploy_sequence(
    commands: list[str],
    config: ProjectConfig,
    sequence_id: str,
    *,
    stop_on_failure: bool = True,
) -> Generator[CommandResultPayload, None, None]:
    """Execute commands in order, yielding each result.

    Validates all command names upfront before executing anything.
    Each step gets a unique request_id; all share the sequence_id.
    """
    # Validate all names first — fail fast on typos
    for name in commands:
        get_command(name)

    for name in commands:
        result = execute_command(name, config, str(uuid.uuid4()), sequence_id)
        yield result
        if stop_on_failure and not result.success:
            return
