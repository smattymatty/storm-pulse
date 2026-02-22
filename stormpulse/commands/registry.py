"""Command whitelist, resolution, and execution."""

from __future__ import annotations

import re
import subprocess
import time
from typing import Any

from stormpulse.config import CommandDef, ProjectConfig
from stormpulse.protocol import CommandResultPayload


class CommandError(Exception):
    """Raised for unknown command names or invalid registry state."""


class ParamValidationError(Exception):
    """Raised when runtime params fail validation."""


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
        command=[
            "/usr/bin/docker", "compose", "--env-file", "{env_file}",
            "-f", "{compose_file}", "build",
        ],
        timeout=300,
        description="Build Docker images",
    ),
    "docker_down": CommandDef(
        group="deploy",
        command=[
            "/usr/bin/docker", "compose", "--env-file", "{env_file}",
            "-f", "{compose_file}", "down",
        ],
        timeout=60,
        requires_confirmation=True,
        description="Stop and remove containers",
    ),
    "docker_up": CommandDef(
        group="deploy",
        command=[
            "/usr/bin/docker", "compose", "--env-file", "{env_file}",
            "-f", "{compose_file}", "up", "-d",
        ],
        timeout=120,
        description="Start containers in detached mode",
    ),
    "django_migrate": CommandDef(
        group="deploy",
        command=[
            "/usr/bin/docker", "compose", "--env-file", "{env_file}",
            "-f", "{compose_file}",
            "exec", "{docker_service_name}", "python", "manage.py", "migrate",
        ],
        timeout=120,
        description="Run Django database migrations",
    ),
    "docker_logs": CommandDef(
        group="diagnostics",
        command=[
            "/usr/bin/docker", "compose", "--env-file", "{env_file}",
            "-f", "{compose_file}",
            "logs", "--tail", "100", "{docker_service_name}",
        ],
        timeout=30,
        description="Show last 100 lines of service logs",
    ),
}


def build_registry(
    config_commands: dict[str, CommandDef],
    disabled: frozenset[str] = frozenset(),
) -> dict[str, CommandDef]:
    """Merge built-in commands with config-defined commands.

    Config commands override built-ins on name collision.
    Commands in *disabled* are removed from the final registry.
    """
    merged = {**COMMAND_REGISTRY, **config_commands}
    return {k: v for k, v in merged.items() if k not in disabled}


# ---------------------------------------------------------------------------
# Resolution and lookup
# ---------------------------------------------------------------------------


def validate_params(
    cmd_def: CommandDef,
    runtime_params: dict[str, str],
) -> dict[str, str]:
    """Validate runtime params against a command's ParamDefs.

    Returns merged dict: ParamDef defaults overridden by valid runtime params.
    Raises ParamValidationError for unknown params, pattern mismatches,
    or missing values (param has no default and no runtime override).
    """
    unknown = set(runtime_params) - set(cmd_def.params)
    if unknown:
        raise ParamValidationError(
            f"Unknown params: {', '.join(sorted(unknown))}"
        )

    merged: dict[str, str] = {}
    for name, pdef in cmd_def.params.items():
        if name in runtime_params:
            value = runtime_params[name]
        elif pdef.default is not None:
            value = pdef.default
        else:
            raise ParamValidationError(
                f"Param {name!r} has no default and was not provided"
            )
        if not re.fullmatch(pdef.pattern, value):
            raise ParamValidationError(
                f"Param {name!r} value {value!r} "
                f"does not match pattern {pdef.pattern!r}"
            )
        merged[name] = value

    return merged


def _resolve_command(
    template: list[str],
    config: ProjectConfig,
    param_overrides: dict[str, str] | None = None,
) -> list[str]:
    """Replace placeholders with values from local config and param overrides.

    Optional placeholders (like ``{env_file}``) resolve to ``""`` when unset.
    Any ``--flag ""`` pair where the value is empty is stripped from the result.

    Raises ValueError on unknown placeholders.
    """
    replacements: dict[str, Any] = {
        "project_dir": str(config.project_dir),
        "compose_file": str(config.compose_file),
        "docker_service_name": config.docker_service_name,
        "env_file": str(config.env_file) if config.env_file else "",
    }
    if param_overrides:
        replacements.update(param_overrides)
    resolved: list[str] = []
    for part in template:
        try:
            resolved.append(part.format_map(replacements))
        except KeyError as exc:
            raise ValueError(f"Unknown placeholder in command template: {exc}") from exc

    # Strip --flag + value pairs where the value resolved to empty.
    cleaned: list[str] = []
    skip_next = False
    for i, arg in enumerate(resolved):
        if skip_next:
            skip_next = False
            continue
        if arg.startswith("--") and i + 1 < len(resolved) and resolved[i + 1] == "":
            skip_next = True
            continue
        cleaned.append(arg)
    return cleaned


def get_command(name: str, *, registry: dict[str, CommandDef]) -> CommandDef:
    """Look up a command by name, or raise CommandError."""
    try:
        return registry[name]
    except KeyError:
        valid = ", ".join(sorted(registry))
        raise CommandError(f"Unknown command: {name!r}. Valid commands: {valid}")


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def execute_command(
    command_name: str,
    config: ProjectConfig,
    request_id: str,
    sequence_id: str | None = None,
    *,
    registry: dict[str, CommandDef],
    runtime_params: dict[str, str] | None = None,
) -> CommandResultPayload:
    """Execute a whitelisted command and return the result.

    Raises CommandError for unknown commands. Raises ParamValidationError
    for invalid params. All other failures (timeout, missing binary,
    OS errors) are reported in the result payload.
    """
    cmd_def = get_command(command_name, registry=registry)
    validated = validate_params(cmd_def, runtime_params or {}) if cmd_def.params else None
    resolved = _resolve_command(cmd_def.command, config, validated)

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
        partial_stderr = str(exc.stderr or "")
        timeout_msg = f"Command timed out after {cmd_def.timeout}s"
        stderr = f"{timeout_msg}\n{partial_stderr}" if partial_stderr else timeout_msg
        return CommandResultPayload(
            request_id=request_id,
            command=command_name,
            group=cmd_def.group,
            success=False,
            exit_code=-1,
            stdout=str(exc.stdout or ""),
            stderr=stderr,
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
