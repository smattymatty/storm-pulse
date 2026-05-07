"""Garage-specific whitelisted commands.

Most commands resolve to: docker exec <container> /garage <subcommand>
with absolute paths and shell=False.

Exception: ``garage_refresh`` is an internal command handled directly
by the agent — it triggers immediate state collection and metrics push
without executing a subprocess.
"""

from __future__ import annotations

from stormpulse.config import CommandDef, GarageConfig, ParamDef

_BUCKET_NAME_PATTERN = r"[a-zA-Z0-9_][a-zA-Z0-9_-]*"
_KEY_NAME_PATTERN = r"[a-zA-Z0-9_][a-zA-Z0-9_-]*"
_KEY_ID_PATTERN = r"[a-zA-Z0-9]+"
_DOCUMENT_PATTERN = r"[a-zA-Z0-9._/-]+"


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
        "garage_bucket_allow_rw": CommandDef(
            group="garage",
            command=[docker, "exec", container, garage, "bucket", "allow",
                     "--read", "--write",
                     "{bucket_name}", "--key", "{key_id}"],
            timeout=15,
            description="Grant read-write access to a bucket for a key",
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
        "garage_bucket_allow_ro": CommandDef(
            group="garage",
            command=[docker, "exec", container, garage, "bucket", "allow",
                     "--read",
                     "{bucket_name}", "--key", "{key_id}"],
            timeout=15,
            description="Grant read-only access to a bucket for a key",
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
        "garage_bucket_website_allow": CommandDef(
            group="garage",
            command=[docker, "exec", container, garage,
                     "bucket", "website", "--allow", "{bucket_name}",
                     "--index-document", "{index_document}",
                     "--error-document", "{error_document}"],
            timeout=30,
            description="Enable static website hosting on a bucket",
            params={
                "bucket_name": ParamDef(
                    placeholder="bucket_name",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="Bucket name or alias",
                ),
                "index_document": ParamDef(
                    placeholder="index_document",
                    default="index.html",
                    pattern=_DOCUMENT_PATTERN,
                    description="Index document filename",
                ),
                "error_document": ParamDef(
                    placeholder="error_document",
                    default="404.html",
                    pattern=_DOCUMENT_PATTERN,
                    description="Error document filename",
                ),
            },
        ),
        "garage_bucket_website_deny": CommandDef(
            group="garage",
            command=[docker, "exec", container, garage,
                     "bucket", "website", "--deny", "{bucket_name}"],
            timeout=30,
            requires_confirmation=True,
            description="Disable static website hosting on a bucket",
            params={
                "bucket_name": ParamDef(
                    placeholder="bucket_name",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="Bucket name or alias",
                ),
            },
        ),
        "garage_bucket_alias_global_add": CommandDef(
            group="garage",
            command=[docker, "exec", container, garage, "bucket", "alias",
                     "{bucket_name}", "{new_alias}"],
            timeout=15,
            description="Add a global alias to a bucket",
            params={
                "bucket_name": ParamDef(
                    placeholder="bucket_name",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="Bucket reference: existing global alias or hex UUID",
                ),
                "new_alias": ParamDef(
                    placeholder="new_alias",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="New global alias to add",
                ),
            },
        ),
        "garage_bucket_alias_global_remove": CommandDef(
            group="garage",
            command=[docker, "exec", container, garage, "bucket", "unalias",
                     "{alias_name}"],
            timeout=15,
            requires_confirmation=True,
            description="Remove a global alias from a bucket",
            params={
                "alias_name": ParamDef(
                    placeholder="alias_name",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="Global alias to remove",
                ),
            },
        ),
        "garage_bucket_alias_local_add": CommandDef(
            group="garage",
            command=[docker, "exec", container, garage, "bucket", "alias",
                     "--local", "{key_id}",
                     "{bucket_name}", "{new_alias}"],
            timeout=15,
            description="Add a local alias scoped to an access key",
            params={
                "key_id": ParamDef(
                    placeholder="key_id",
                    default=None,
                    pattern=_KEY_ID_PATTERN,
                    description="Access key the local alias is scoped to",
                ),
                "bucket_name": ParamDef(
                    placeholder="bucket_name",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="Bucket reference: existing global alias or hex UUID",
                ),
                "new_alias": ParamDef(
                    placeholder="new_alias",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="New local alias to add",
                ),
            },
        ),
        "garage_bucket_alias_local_remove": CommandDef(
            group="garage",
            command=[docker, "exec", container, garage, "bucket", "unalias",
                     "--local", "{key_id}",
                     "{alias_name}"],
            timeout=15,
            requires_confirmation=True,
            description="Remove a local alias scoped to an access key",
            params={
                "key_id": ParamDef(
                    placeholder="key_id",
                    default=None,
                    pattern=_KEY_ID_PATTERN,
                    description="Access key the local alias is scoped to",
                ),
                "alias_name": ParamDef(
                    placeholder="alias_name",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="Local alias to remove",
                ),
            },
        ),
        # ----- Internal -----
        "garage_refresh": CommandDef(
            group="garage",
            command=["garage_refresh"],  # internal — not a subprocess
            timeout=30,
            description="Internal command — triggers immediate state collection and metrics push",
        ),
        "garage_provision_customer_bucket": CommandDef(
            group="garage",
            command=["garage_provision_customer_bucket"],  # internal — handled by JobManager
            timeout=600,  # per-step reference; long_running ignores it for total duration
            description="Orchestrated bucket provisioning: create bucket, three keys, attach local aliases. Atomic with rollback.",
            sensitive_output=True,  # secrets ride in stdout/extras
            long_running=True,
            params={
                "display_name": ParamDef(
                    placeholder="display_name",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="Customer-facing bucket name; becomes the local alias on each key",
                ),
                "key_name_admin": ParamDef(
                    placeholder="key_name_admin",
                    default=None,
                    pattern=_KEY_NAME_PATTERN,
                    description="Garage key name for the admin (all-permissions) key",
                ),
                "key_name_rw": ParamDef(
                    placeholder="key_name_rw",
                    default=None,
                    pattern=_KEY_NAME_PATTERN,
                    description="Garage key name for the read-write key",
                ),
                "key_name_ro": ParamDef(
                    placeholder="key_name_ro",
                    default=None,
                    pattern=_KEY_NAME_PATTERN,
                    description="Garage key name for the read-only key",
                ),
            },
        ),
        "garage_rotate_customer_key": CommandDef(
            group="garage",
            command=["garage_rotate_customer_key"],  # internal — handled by JobManager
            timeout=120,
            description="Orchestrated key rotation: create new key, attach local alias, delete old key. Atomic with rollback.",
            sensitive_output=True,  # secrets ride in stdout/extras
            long_running=True,
            params={
                "old_key_id": ParamDef(
                    placeholder="old_key_id",
                    default=None,
                    pattern=_KEY_ID_PATTERN,
                    description="Garage ID of the key being rotated out",
                ),
                "new_key_name": ParamDef(
                    placeholder="new_key_name",
                    default=None,
                    pattern=_KEY_NAME_PATTERN,
                    description="Name for the replacement key",
                ),
                "bucket_id": ParamDef(
                    placeholder="bucket_id",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="Bucket UUID the local alias is attached to",
                ),
                "local_alias": ParamDef(
                    placeholder="local_alias",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="Local alias to re-attach on the new key",
                ),
                "key_tier": ParamDef(
                    placeholder="key_tier",
                    default=None,
                    pattern=r"(?:all|rw|ro)",
                    description="Permission tier for the new key: 'all', 'rw', or 'ro'",
                ),
            },
        ),
        "garage_bucket_clear": CommandDef(
            group="garage",
            command=["garage_bucket_clear"],  # internal — handled by JobManager, not a subprocess
            timeout=600,  # per-batch reference; long_running ignores it for total duration
            description="Bulk-delete every object in a bucket via the local Garage S3 endpoint",
            requires_confirmation=True,
            sensitive_output=True,  # the secret arrives in params; never log them
            long_running=True,
            params={
                "bucket_name": ParamDef(
                    placeholder="bucket_name",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="Bucket to clear",
                ),
                "s3_endpoint": ParamDef(
                    placeholder="s3_endpoint",
                    default=None,
                    pattern=r"https?://[a-zA-Z0-9.-]+(:[0-9]+)?",
                    description="Garage S3 endpoint URL (no path/query)",
                ),
                "region": ParamDef(
                    placeholder="region",
                    default=None,
                    pattern=r"[a-zA-Z0-9_-]+",
                    description="S3 region for SigV4 signing",
                ),
                "access_key_id": ParamDef(
                    placeholder="access_key_id",
                    default=None,
                    pattern=_KEY_ID_PATTERN,
                    description="Customer S3 access key ID",
                ),
                "secret_access_key": ParamDef(
                    placeholder="secret_access_key",
                    default=None,
                    pattern=r".+",
                    description="Customer S3 secret. Held in agent process memory only for the job's lifetime.",
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
