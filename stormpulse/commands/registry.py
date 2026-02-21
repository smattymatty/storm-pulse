"""Command whitelist, resolution, and execution."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import Any

from stormpulse.config import ProjectConfig
from stormpulse.protocol import CommandResultPayload


class CommandError(Exception):
    """Raised for unknown command names or invalid registry state."""


@dataclass(frozen=True, slots=True)
class CommandDef:
    """A single whitelisted command definition."""

    group: str
    command: list[str]
    timeout: int
    requires_confirmation: bool = False
    description: str = ""


# ---------------------------------------------------------------------------
# The whitelist — data, not code (Rule of Representation)
# ---------------------------------------------------------------------------

COMMAND_REGISTRY: dict[str, CommandDef] = {
    "git_pull": CommandDef(
        group="deploy",
        command=["/usr/bin/git", "-C", "{project_dir}", "pull"],
        timeout=60,
        description="Pull latest changes from remote",
    ),
    "docker_build": CommandDef(
        group="deploy",
        command=["/usr/bin/docker", "compose", "-f", "{compose_file}", "build"],
        timeout=300,
        description="Build Docker images",
    ),
    "docker_down": CommandDef(
        group="deploy",
        command=["/usr/bin/docker", "compose", "-f", "{compose_file}", "down"],
        timeout=60,
        requires_confirmation=True,
        description="Stop and remove containers",
    ),
    "docker_up": CommandDef(
        group="deploy",
        command=["/usr/bin/docker", "compose", "-f", "{compose_file}", "up", "-d"],
        timeout=120,
        description="Start containers in detached mode",
    ),
    "django_migrate": CommandDef(
        group="deploy",
        command=[
            "/usr/bin/docker", "compose", "-f", "{compose_file}",
            "exec", "{docker_service_name}", "python", "manage.py", "migrate",
        ],
        timeout=120,
        description="Run Django database migrations",
    ),
}


# ---------------------------------------------------------------------------
# Resolution and lookup
# ---------------------------------------------------------------------------


def _resolve_command(template: list[str], config: ProjectConfig) -> list[str]:
    """Replace placeholders with values from local config.

    Raises ValueError on unknown placeholders.
    """
    replacements: dict[str, Any] = {
        "project_dir": str(config.project_dir),
        "compose_file": str(config.compose_file),
        "docker_service_name": config.docker_service_name,
    }
    resolved: list[str] = []
    for part in template:
        try:
            resolved.append(part.format_map(replacements))
        except KeyError as exc:
            raise ValueError(f"Unknown placeholder in command template: {exc}") from exc
    return resolved


def get_command(name: str) -> CommandDef:
    """Look up a command by name, or raise CommandError."""
    try:
        return COMMAND_REGISTRY[name]
    except KeyError:
        valid = ", ".join(sorted(COMMAND_REGISTRY))
        raise CommandError(f"Unknown command: {name!r}. Valid commands: {valid}")


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def execute_command(
    command_name: str,
    config: ProjectConfig,
    request_id: str,
    sequence_id: str | None = None,
) -> CommandResultPayload:
    """Execute a whitelisted command and return the result.

    Raises CommandError for unknown commands. All other failures
    (timeout, missing binary, OS errors) are reported in the result payload.
    """
    cmd_def = get_command(command_name)
    resolved = _resolve_command(cmd_def.command, config)

    start = time.monotonic()
    try:
        proc = subprocess.run(
            resolved,
            capture_output=True,
            text=True,
            timeout=cmd_def.timeout,
            shell=False,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        return CommandResultPayload(
            request_id=request_id,
            command=command_name,
            group=cmd_def.group,
            success=proc.returncode == 0,
            exit_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            duration_ms=duration_ms,
            sequence_id=sequence_id,
            failure_reason=None if proc.returncode == 0 else "exit_code",
        )
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        return CommandResultPayload(
            request_id=request_id,
            command=command_name,
            group=cmd_def.group,
            success=False,
            exit_code=-1,
            stdout=str(exc.stdout or ""),
            stderr=str(exc.stderr or ""),
            duration_ms=duration_ms,
            sequence_id=sequence_id,
            failure_reason="timeout",
        )
    except FileNotFoundError:
        duration_ms = int((time.monotonic() - start) * 1000)
        return CommandResultPayload(
            request_id=request_id,
            command=command_name,
            group=cmd_def.group,
            success=False,
            exit_code=-1,
            stdout="",
            stderr=f"Binary not found: {resolved[0]}",
            duration_ms=duration_ms,
            sequence_id=sequence_id,
            failure_reason="not_found",
        )
    except OSError as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        return CommandResultPayload(
            request_id=request_id,
            command=command_name,
            group=cmd_def.group,
            success=False,
            exit_code=-1,
            stdout="",
            stderr=str(exc),
            duration_ms=duration_ms,
            sequence_id=sequence_id,
            failure_reason="os_error",
        )
