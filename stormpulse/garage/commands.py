"""Garage-specific whitelisted commands.

Most commands resolve to: docker exec <container> /garage <subcommand>
with absolute paths and shell=False.

Exception: ``garage_refresh`` is an internal command handled directly
by the agent — it triggers immediate state collection and metrics push
without executing a subprocess.
"""

from __future__ import annotations

from stormpulse.config import CommandDef, GarageConfig, ParamDef

# S3-strict bucket name (which Garage's bucket-create validator
# enforces): 3-63 chars, lowercase alphanumeric + hyphens, must start
# AND end alphanumeric. Garage CLI rejects names with leading
# underscores, uppercase, or any underscore at all on S3-strict
# deployments — see ``provision_bucket.py``'s throwaway-alias comment
# for the empirical lesson. Matches the 16-char bucket-UUID prefix
# (lowercase hex) too, so this pattern serves both display-name
# validation AND bucket_id-as-reference validation in commands like
# ``garage_delete_provisioned_bucket``.
_BUCKET_NAME_PATTERN = r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]"
# Key names are not S3-bucket-shaped; Garage allows the broader set.
# Storm provisions keys as ``usr-<pk>-<bucket>-<tier>`` which uses
# hyphens only, but other ops paths may include underscores or mixed
# case for descriptive names.
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
            description="Orchestrated bucket + admin key provisioning. Atomic with rollback. The rw/ro keys are added on demand via garage_provision_additional_key.",
            sensitive_output=True,  # secrets ride in stdout/extras
            long_running=True,
            params={
                "display_name": ParamDef(
                    placeholder="display_name",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="Customer-facing bucket name; becomes the local alias on the admin key",
                ),
                "key_name_admin": ParamDef(
                    placeholder="key_name_admin",
                    default=None,
                    pattern=_KEY_NAME_PATTERN,
                    description="Garage key name for the admin (all-permissions) key",
                ),
            },
        ),
        "garage_delete_provisioned_bucket": CommandDef(
            group="garage",
            command=["garage_delete_provisioned_bucket"],  # internal — handled by JobManager
            timeout=120,
            requires_confirmation=True,
            description="Orchestrated bucket deletion: detaches all aliases (using a temp global to bypass the orphan-rule deadlock when only locals exist), then deletes. Atomic with rollback.",
            long_running=True,
            params={
                "bucket_id": ParamDef(
                    placeholder="bucket_id",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="Bucket UUID (16-char Garage ID) to delete",
                ),
            },
        ),
        "garage_provision_additional_key": CommandDef(
            group="garage",
            command=["garage_provision_additional_key"],  # internal — handled by JobManager
            timeout=120,
            description="Orchestrated provisioning of an additional rw or ro key on an existing bucket. Atomic with rollback.",
            sensitive_output=True,  # the new secret rides in stdout/extras
            long_running=True,
            params={
                "new_key_name": ParamDef(
                    placeholder="new_key_name",
                    default=None,
                    pattern=_KEY_NAME_PATTERN,
                    description="Garage key name for the new tiered key",
                ),
                "bucket_id": ParamDef(
                    placeholder="bucket_id",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="Bucket UUID (16-char Garage ID) to attach the local alias to",
                ),
                "local_alias": ParamDef(
                    placeholder="local_alias",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="Local alias to attach on the new key (typically display_name)",
                ),
                "key_tier": ParamDef(
                    placeholder="key_tier",
                    default=None,
                    pattern=r"(?:rw|ro)",
                    description="Permission tier for the new key: 'rw' or 'ro'. Admin is created at provision time.",
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
                    pattern=r"^https?://[a-zA-Z0-9.-]+(:[0-9]+)?$",
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
        "garage_bucket_set_cors": CommandDef(
            group="garage",
            command=["garage_bucket_set_cors"],  # internal — handled by JobManager, not a subprocess
            timeout=30,  # single API call; long_running ignores this for total duration
            description="Apply the platform-default CORS rule to a bucket via the local Garage S3 endpoint",
            sensitive_output=True,  # the secret arrives in params; never log them
            long_running=True,
            params={
                "bucket_name": ParamDef(
                    placeholder="bucket_name",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="Bucket to apply CORS to",
                ),
                "s3_endpoint": ParamDef(
                    placeholder="s3_endpoint",
                    default=None,
                    pattern=r"^https?://[a-zA-Z0-9.-]+(:[0-9]+)?$",
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
                "origins": ParamDef(
                    placeholder="origins",
                    default=None,
                    pattern=r"^\[.+\]$",
                    description=(
                        "JSON-encoded list of allowed origins, e.g. "
                        "'[\"https://stormdevelopments.ca\"]'. The handler "
                        "decodes via json.loads and validates list[str]."
                    ),
                ),
            },
        ),
        "garage_walk_bucket_stats": CommandDef(
            group="garage",
            command=["garage_walk_bucket_stats"],  # internal — handled by JobManager
            timeout=120,  # 100k objects ÷ 1000/page = 100 pages; ~1s/page p95
            description=(
                "Walk a bucket on the local Garage S3 endpoint to compute "
                "per-prefix object count + byte sum. Customer credentials "
                "in params; agent signs requests against loopback so the "
                "customer's home IP doesn't appear in Garage's access log."
            ),
            sensitive_output=True,  # the secret arrives in params
            long_running=True,
            params={
                "bucket_name": ParamDef(
                    placeholder="bucket_name",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="Bucket to walk (local alias = display_name)",
                ),
                "s3_endpoint": ParamDef(
                    placeholder="s3_endpoint",
                    default=None,
                    pattern=r"^https?://[a-zA-Z0-9.-]+(:[0-9]+)?$",
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
                    description="Customer S3 access key ID (any tier)",
                ),
                "secret_access_key": ParamDef(
                    placeholder="secret_access_key",
                    default=None,
                    pattern=r".+",
                    description="Customer S3 secret. In agent memory only for the job lifetime.",
                ),
                "prefix": ParamDef(
                    placeholder="prefix",
                    default="",
                    # S3 prefix — empty (root) or any path-safe sequence
                    # ending in /. Validated stricter by the Storm-side
                    # _validate_listing_prefix before dispatch reaches here.
                    pattern=r"|[A-Za-z0-9_\-./]+/",
                    description="Prefix to walk under; '' = bucket root",
                ),
                "max_objects": ParamDef(
                    placeholder="max_objects",
                    default="100000",
                    pattern=r"[0-9]{1,7}",
                    description="Cap; truncated=True returned if exceeded",
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
