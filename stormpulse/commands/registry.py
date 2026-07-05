"""Command whitelist, resolution, and execution."""

from __future__ import annotations

import logging
import re
import subprocess
import time
from typing import Any

from stormpulse.config import CommandSpec, ParamDef, ProjectConfig
from stormpulse.protocol import CommandResultPayload

logger = logging.getLogger(__name__)


class CommandError(Exception):
    """Raised for unknown command names or invalid registry state."""


class ParamValidationError(Exception):
    """Raised when runtime params fail validation."""


COMMAND_REGISTRY: dict[str, CommandSpec] = {
    "git_pull": CommandSpec(
        group="deploy",
        command=["/usr/bin/git", "-C", "{project_dir}", "pull"],
        timeout=60,
        description="Pull latest changes from remote",
    ),
    "docker_logs": CommandSpec(
        group="diagnostics",
        command=[
            "/usr/bin/docker",
            "compose",
            "--env-file",
            "{env_file}",
            "-f",
            "{compose_file}",
            "logs",
            "--tail",
            "{tail_lines}",
            "{docker_service_name}",
        ],
        timeout=30,
        description="Show recent service logs",
        params={
            "docker_service_name": ParamDef(
                placeholder="docker_service_name",
                default=None,
                pattern="[a-zA-Z0-9_-]+",
                description="Docker Compose service name",
            ),
            "tail_lines": ParamDef(
                placeholder="tail_lines",
                default="100",
                pattern="[0-9]{1,5}",
                description="Number of log lines to show",
            ),
        },
    ),
    # Dashboard-driven verify-block execution for the sign-off checklist
    # feature in the Storm Developments website. Unlike the other entries
    # in this registry whose shell templates are baked in here, this one
    # takes the shell text as a parameter (HMAC-signed by the dashboard).
    # The trust shift is intentional and documented in the storm-pulse
    # 0.1.8 CHANGELOG: the agent's job is faithful execution of
    # dashboard-signed commands, not to be a defense-in-depth layer
    # against a compromised dashboard. Confined to read-only verify
    # checks by the dashboard side (the website refuses to dispatch
    # any block whose `kind != verify`).
    "run_verify_block": CommandSpec(
        group="signoff",
        command=["/bin/bash", "-c", "{verify_command}"],
        timeout=30,
        description="Run a sign-off verify command from the dashboard",
        params={
            "verify_command": ParamDef(
                placeholder="verify_command",
                default=None,
                max_bytes=4096,
                description="Shell command text supplied by the dashboard",
            ),
        },
    ),
    # Sibling of run_verify_block for the dashboard's apply-block path.
    # Same HMAC-signed-shell trust shift (see the run_verify_block
    # comment above); the parameter name, timeout, and byte cap differ
    # because apply scripts include long-running work (docker pull,
    # vulnerability scans, image builds) and multi-line heredocs that
    # the verify limits were never sized for. Seal-gated identically to
    # run_verify_block, see build_registry below.
    "run_apply_block": CommandSpec(
        group="signoff",
        command=["/bin/bash", "-c", "{apply_command}"],
        timeout=600,
        description="Run a sign-off apply command from the dashboard",
        params={
            "apply_command": ParamDef(
                placeholder="apply_command",
                default=None,
                max_bytes=16384,
                description="Shell command text supplied by the dashboard",
            ),
        },
    ),
}


def build_registry(
    config_commands: dict[str, CommandSpec],
    disabled: frozenset[str] = frozenset(),
    *,
    signoff_sealed: bool = False,
) -> dict[str, CommandSpec]:
    """Merge built-in commands with config-defined commands.

    Config commands override built-ins on name collision.
    Commands in *disabled* are removed from the final registry.

    When ``signoff_sealed`` is true, both ``run_verify_block`` and
    ``run_apply_block`` are excluded. A sealed agent advertises (and
    accepts) the same pre-0.1.8 capability set, so the dashboard's
    verify and apply hatches are both gone until the operator unseals
    on the host. See ``stormpulse.signoff`` and ADR CORE-004.
    """
    merged = {**COMMAND_REGISTRY, **config_commands}
    auto_disabled = {"run_verify_block", "run_apply_block"} if signoff_sealed else set()
    return {
        k: v for k, v in merged.items() if k not in disabled and k not in auto_disabled
    }


def validate_params(
    cmd_def: CommandSpec,
    runtime_params: dict[str, str],
) -> dict[str, str]:
    """Validate runtime params against a command's ParamDefs.

    Returns merged dict: ParamDef defaults overridden by valid runtime params.
    Raises ParamValidationError for unknown params, pattern mismatches,
    or missing values (param has no default and no runtime override).
    """
    unknown = set(runtime_params) - set(cmd_def.params)
    if unknown:
        raise ParamValidationError(f"Unknown params: {', '.join(sorted(unknown))}")

    merged: dict[str, str] = {}
    for name, pdef in cmd_def.params.items():
        if name in runtime_params:
            value = runtime_params[name]
        elif pdef.default is not None:
            value = pdef.default
        else:
            continue  # no static default - config provides the fallback
        # Regex validation: short identifiers, bucket names, key IDs.
        if pdef.pattern is not None:
            if not re.fullmatch(pdef.pattern, value):
                raise ParamValidationError(
                    f"Param {name!r} value {value!r} "
                    f"does not match pattern {pdef.pattern!r}"
                )
        # Byte-cap validation: opaque content blobs like a Caddyfile
        # fragment. Counts UTF-8 bytes (not characters) to match what
        # actually traverses the wire.
        if pdef.max_bytes is not None:
            byte_size = len(value.encode("utf-8"))
            if byte_size > pdef.max_bytes:
                raise ParamValidationError(
                    f"Param {name!r} is {byte_size} bytes, "
                    f"exceeds max_bytes={pdef.max_bytes}"
                )
        merged[name] = value

    return merged


def non_secret_params(
    cmd_def: CommandSpec,
    params: dict[str, str],
) -> dict[str, str]:
    """Drop ``secret``-flagged params, for event/log context. The handler keeps the full set."""
    return {
        name: value
        for name, value in params.items()
        if not (name in cmd_def.params and cmd_def.params[name].secret)
    }


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


def get_command(name: str, *, registry: dict[str, CommandSpec]) -> CommandSpec:
    """Look up a command by name, or raise CommandError."""
    try:
        return registry[name]
    except KeyError:
        valid = ", ".join(sorted(registry))
        raise CommandError(f"Unknown command: {name!r}. Valid commands: {valid}")


def execute_command(
    command_name: str,
    config: ProjectConfig,
    request_id: str,
    sequence_id: str | None = None,
    *,
    registry: dict[str, CommandSpec],
    runtime_params: dict[str, str] | None = None,
) -> CommandResultPayload:
    """Execute a whitelisted command and return the result.

    Raises CommandError for unknown commands. Raises ParamValidationError
    for invalid params. All other failures (timeout, missing binary,
    OS errors) are reported in the result payload.
    """
    cmd_def = get_command(command_name, registry=registry)
    validated = (
        validate_params(cmd_def, runtime_params or {}) if cmd_def.params else None
    )
    resolved = _resolve_command(cmd_def.command, config, validated)

    if not cmd_def.sensitive_output:
        logger.debug("Running command %r: %s", command_name, resolved)

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
