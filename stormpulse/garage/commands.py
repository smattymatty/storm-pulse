"""Garage-specific whitelisted commands.

All commands resolve to: docker exec <container> /garage <subcommand>
with absolute paths and shell=False.
"""

from __future__ import annotations

from stormpulse.config import CommandDef, GarageConfig, ParamDef

_BUCKET_NAME_PATTERN = r"[a-zA-Z0-9_-]+"
_KEY_NAME_PATTERN = r"[a-zA-Z0-9_-]+"
_KEY_ID_PATTERN = r"[a-zA-Z0-9]+"


def build_garage_commands(config: GarageConfig) -> dict[str, CommandDef]:
    """Build Garage command registry from config.

    Uses config.docker_binary, config.container_name, and config.garage_binary
    to construct the full command templates.
    """
    docker = config.docker_binary
    container = config.container_name
    garage = config.garage_binary

    return {
        # ----- Read-only -----
        "garage_status": CommandDef(
            group="garage",
            command=[docker, "exec", container, garage, "status"],
            timeout=15,
            description="Show Garage node status",
        ),
        "garage_stats": CommandDef(
            group="garage",
            command=[docker, "exec", container, garage, "stats"],
            timeout=15,
            description="Show Garage cluster statistics",
        ),
        "garage_bucket_list": CommandDef(
            group="garage",
            command=[docker, "exec", container, garage, "bucket", "list"],
            timeout=15,
            description="List all Garage buckets",
        ),
        "garage_bucket_info": CommandDef(
            group="garage",
            command=[docker, "exec", container, garage, "bucket", "info", "{bucket_name}"],
            timeout=15,
            description="Show bucket details",
            params={
                "bucket_name": ParamDef(
                    placeholder="bucket_name",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="Bucket name or alias",
                ),
            },
        ),
        "garage_key_list": CommandDef(
            group="garage",
            command=[docker, "exec", container, garage, "key", "list"],
            timeout=15,
            description="List all Garage API keys",
        ),
        # ----- State-changing -----
        "garage_bucket_create": CommandDef(
            group="garage",
            command=[docker, "exec", container, garage, "bucket", "create", "{bucket_name}"],
            timeout=15,
            description="Create a new bucket",
            params={
                "bucket_name": ParamDef(
                    placeholder="bucket_name",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="Name for the new bucket",
                ),
            },
        ),
        "garage_bucket_delete": CommandDef(
            group="garage",
            command=[docker, "exec", container, garage, "bucket", "delete", "--yes",
                     "{bucket_name}"],
            timeout=15,
            requires_confirmation=True,
            description="Delete a bucket",
            params={
                "bucket_name": ParamDef(
                    placeholder="bucket_name",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="Bucket to delete",
                ),
            },
        ),
        "garage_key_create": CommandDef(
            group="garage",
            command=[docker, "exec", container, garage, "key", "create", "{key_name}"],
            timeout=15,
            description="Create a new API key",
            sensitive_output=True,
            params={
                "key_name": ParamDef(
                    placeholder="key_name",
                    default=None,
                    pattern=_KEY_NAME_PATTERN,
                    description="Name for the new key",
                ),
            },
        ),
        "garage_key_delete": CommandDef(
            group="garage",
            command=[docker, "exec", container, garage, "key", "delete", "--yes", "{key_id}"],
            timeout=15,
            requires_confirmation=True,
            description="Delete an API key",
            params={
                "key_id": ParamDef(
                    placeholder="key_id",
                    default=None,
                    pattern=_KEY_ID_PATTERN,
                    description="Key ID to delete",
                ),
            },
        ),
        "garage_bucket_allow": CommandDef(
            group="garage",
            command=[docker, "exec", container, garage, "bucket", "allow",
                     "--read", "--write", "--owner",
                     "{bucket_name}", "--key", "{key_id}"],
            timeout=15,
            description="Grant full access to a bucket for a key",
            params={
                "bucket_name": ParamDef(
                    placeholder="bucket_name",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="Bucket to grant access to",
                ),
                "key_id": ParamDef(
                    placeholder="key_id",
                    default=None,
                    pattern=_KEY_ID_PATTERN,
                    description="Key to grant access for",
                ),
            },
        ),
        "garage_bucket_deny": CommandDef(
            group="garage",
            command=[docker, "exec", container, garage, "bucket", "deny",
                     "--read", "--write", "--owner",
                     "{bucket_name}", "--key", "{key_id}"],
            timeout=15,
            requires_confirmation=True,
            description="Revoke all access to a bucket for a key",
            params={
                "bucket_name": ParamDef(
                    placeholder="bucket_name",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="Bucket to revoke access from",
                ),
                "key_id": ParamDef(
                    placeholder="key_id",
                    default=None,
                    pattern=_KEY_ID_PATTERN,
                    description="Key to revoke access for",
                ),
            },
        ),
    }
