"""Build the register-payload metadata describing the agent's command surface.

The dashboard renders one entry per command using this metadata: which params
exist, their defaults and regex constraints, whether the command needs
confirmation, whether it streams (``long_running``), and the template the
operator sees when they hover. Binary paths are stripped to the basename for
display — ``/usr/bin/docker`` becomes ``docker`` — so the rendered template
matches the operator's mental model rather than the agent's filesystem.
"""

from __future__ import annotations

from typing import Any

from stormpulse.config import CommandDef, ProjectConfig


def strip_binary_path(arg: str) -> str:
    """Strip the directory off an absolute binary path for display.

    ``/usr/bin/docker`` → ``docker``; flags, relative names, and ``{placeholder}``
    tokens are returned unchanged.
    """
    if arg.startswith("/") and "/" in arg[1:]:
        return arg.rsplit("/", 1)[1]
    return arg


def build_commands_metadata(
    registry: dict[str, CommandDef],
    config: ProjectConfig,
) -> dict[str, Any]:
    """Build the rich command-metadata dict embedded in the register payload.

    Params with no static default fall back to a value from the project
    config (``docker_service_name`` is the only one today). Command keys
    are sorted so dashboards see a stable order across registers.
    """
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
