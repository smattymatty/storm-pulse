"""Build the register-payload command-metadata dict; binary paths stripped to basenames for display."""

from __future__ import annotations

from typing import Any

from stormpulse.config import CommandDef, ProjectConfig


def strip_binary_path(arg: str) -> str:
    """``/usr/bin/docker`` → ``docker``; flags, relative names, and ``{placeholder}`` tokens are returned unchanged."""
    if arg.startswith("/") and "/" in arg[1:]:
        return arg.rsplit("/", 1)[1]
    return arg


def build_commands_metadata(
    registry: dict[str, CommandDef],
    config: ProjectConfig,
) -> dict[str, Any]:
    """Command-metadata dict for the register payload; params fall back to project config defaults, keys sorted."""
    config_defaults: dict[str, str] = {
        "docker_service_name": config.docker_service_name,
    }

    result: dict[str, Any] = {}
    for name, cmd_def in sorted(registry.items()):
        template = [strip_binary_path(part) for part in cmd_def.command]

        params: dict[str, Any] = {}
        for pname, pdef in cmd_def.params.items():
            default = pdef.default
            if default is None:
                default = config_defaults.get(pdef.placeholder)
            params[pname] = {
                "default": default,
                "pattern": pdef.pattern,
                "description": pdef.description,
            }

        result[name] = {
            "group": cmd_def.group,
            "description": cmd_def.description,
            "template": template,
            "timeout": cmd_def.timeout,
            "requires_confirmation": cmd_def.requires_confirmation,
            "long_running": cmd_def.long_running,
            "params": params,
        }
    return result
