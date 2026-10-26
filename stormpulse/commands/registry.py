"""Command whitelist, resolution, and execution."""

from __future__ import annotations

import logging
import re
import subprocess
import time
from typing import Any

from stormpulse.config import CommandSpec, ParamDef, ProjectConfig
from stormpulse.config.param_schema import schema_violation
from stormpulse.protocol import CommandResultPayload, FailureReason
from stormpulse.sdk.declaration import canonical_json

logger = logging.getLogger(__name__)


class CommandError(Exception):
    """The whitelist refused a name; ``reason`` is what the result carries:
    ``unknown_command`` for a name it has never heard of, ``forbidden`` for
    one it knows but will not run in this form."""

    def __init__(
        self, message: str, *, reason: FailureReason = FailureReason.UNKNOWN_COMMAND
    ) -> None:
        super().__init__(message)
        self.reason = reason


class ParamValidationError(Exception):
    """Raised when runtime params fail validation."""


# The shell hatches: argv carries dashboard-supplied shell text, so the seal
# excludes them (CORE-004). Fitness Function 13 holds every shell argv to this set.
VERIFY_BLOCK_COMMAND = "run_verify_block"
APPLY_BLOCK_COMMAND = "run_apply_block"
SEALED_COMMANDS = frozenset({VERIFY_BLOCK_COMMAND, APPLY_BLOCK_COMMAND})


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
    # Unlike the baked templates above, this runs signed shell text from the
    # control plane's sign-off checklist: faithful execution, not a defence
    # against a compromised control plane. Hence the seal (CORE-004).
    VERIFY_BLOCK_COMMAND: CommandSpec(
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
    APPLY_BLOCK_COMMAND: CommandSpec(
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
    auto_disabled = SEALED_COMMANDS if signoff_sealed else frozenset()
    return {
        k: v for k, v in merged.items() if k not in disabled and k not in auto_disabled
    }


def validate_params(
    cmd_def: CommandSpec,
    runtime_params: dict[str, Any],
) -> dict[str, Any]:
    """Validate runtime params against a command's ParamDefs.

    Returns merged dict: ParamDef defaults overridden by valid runtime params.
    Raises ParamValidationError for unknown params, a value of the wrong JSON
    type, a pattern, byte-cap or schema miss (CORE-011 d9).
    """
    if not isinstance(runtime_params, dict) or not all(
        isinstance(key, str) for key in runtime_params
    ):
        raise ParamValidationError("Command params must be an object with string keys")
    unknown = set(runtime_params) - set(cmd_def.params)
    if unknown:
        raise ParamValidationError(f"Unknown params: {', '.join(sorted(unknown))}")

    merged: dict[str, Any] = {}
    for name, pdef in cmd_def.params.items():
        if name in runtime_params:
            value = runtime_params[name]
        elif pdef.default is not None:
            value = pdef.default
        else:
            continue  # no static default - config provides the fallback
        problem = _param_problem(pdef, value)
        if problem is not None:
            raise ParamValidationError(f"Param {name!r} {problem}")
        merged[name] = value

    return merged


def _param_problem(pdef: ParamDef, value: Any) -> str | None:
    """Why ``value`` fails ``pdef``, or None. A secret's value never rides the
    message: it lands in logs and stderr. Shape detail carries paths and rules."""
    return _shape_problem(pdef, value) or _size_problem(pdef, value)


def _shape_problem(pdef: ParamDef, value: Any) -> str | None:
    """Type, then pattern: the checks that read the value as a string."""
    if pdef.schema is None and not isinstance(value, str):
        return f"is {type(value).__name__}, expected {pdef.wire_type}"
    # Regex validation: short identifiers, bucket names, key IDs.
    if pdef.pattern is not None and not re.fullmatch(pdef.pattern, value):
        shown = (
            f"({len(value)} chars, withheld: secret)" if pdef.secret else repr(value)
        )
        return f"value {shown} does not match pattern {pdef.pattern!r}"
    return None


def _size_problem(pdef: ParamDef, value: Any) -> str | None:
    """Byte cap, then schema: the checks that read the value as it rides the wire."""
    # Byte cap: a string as its UTF-8 bytes (what rides the wire; an ASCII escape
    # would inflate it), any other JSON value as its canonical encoding.
    if pdef.max_bytes is not None:
        encoded = value if isinstance(value, str) else canonical_json(value)
        byte_size = len(encoded.encode("utf-8"))
        if byte_size > pdef.max_bytes:
            return f"is {byte_size} bytes, exceeds max_bytes={pdef.max_bytes}"
    if pdef.schema is not None:
        violation = schema_violation(value, pdef.schema)
        if violation is not None:
            return (
                "withheld: secret"
                if pdef.secret
                else f"does not match schema: {violation}"
            )
    return None


def non_secret_params(
    cmd_def: CommandSpec,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Drop ``secret``-flagged params, for event/log context. The handler keeps the full set."""
    return {
        name: value
        for name, value in params.items()
        if not (name in cmd_def.params and cmd_def.params[name].secret)
    }


def _resolve_command(
    template: list[str],
    config: ProjectConfig,
    param_overrides: dict[str, Any] | None = None,
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
    """Look up a subprocess command by name, or raise CommandError.

    A job or refresh spec carries a bare name, not an absolute binary, so
    running one here would hand that name to a PATH lookup.
    """
    try:
        cmd_def = registry[name]
    except KeyError:
        valid = ", ".join(sorted(registry))
        raise CommandError(f"Unknown command: {name!r}. Valid commands: {valid}")
    if cmd_def.mode != "subprocess":
        raise CommandError(
            f"Command {name!r} is a {cmd_def.mode} command; send it on its own, "
            "not in a sequence",
            reason=FailureReason.FORBIDDEN,
        )
    refused = cmd_def.authorize() if cmd_def.authorize is not None else None
    if refused is not None:
        raise CommandError(
            f"Command {name!r} is fenced: {refused}", reason=FailureReason.FORBIDDEN
        )
    return cmd_def


def execute_command(
    command_name: str,
    config: ProjectConfig,
    request_id: str,
    sequence_id: str | None = None,
    *,
    registry: dict[str, CommandSpec],
    runtime_params: dict[str, Any] | None = None,
) -> CommandResultPayload:
    """Execute a whitelisted command and return the result.

    Raises CommandError for unknown commands. Raises ParamValidationError
    for invalid params. All other failures (timeout, missing binary,
    OS errors) are reported in the result payload.
    """
    cmd_def = get_command(command_name, registry=registry)
    validated = validate_params(
        cmd_def, runtime_params if runtime_params is not None else {}
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
            failure_reason=None if proc.returncode == 0 else FailureReason.EXIT_CODE,
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
            failure_reason=FailureReason.TIMEOUT,
        )
    except FileNotFoundError:
        duration_ms = int((time.monotonic() - start) * 1000)
        return CommandResultPayload.failed(
            request_id,
            command_name,
            cmd_def.group,
            FailureReason.NOT_FOUND,
            f"Binary not found: {resolved[0]}",
            sequence_id=sequence_id,
            duration_ms=duration_ms,
        )
    except OSError as exc:
        duration_ms = int((time.monotonic() - start) * 1000)
        return CommandResultPayload.failed(
            request_id,
            command_name,
            cmd_def.group,
            FailureReason.OS_ERROR,
            str(exc),
            sequence_id=sequence_id,
            duration_ms=duration_ms,
        )
