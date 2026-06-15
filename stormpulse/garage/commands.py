"""Garage-specific whitelisted commands.

Most resolve to ``docker exec <container> /garage <subcommand>``, shell=False.
``garage_refresh`` is the exception: an internal command handled directly by
the agent, triggering immediate state collection without a subprocess.

Long-running commands (bucket clear, provisioning, key rotation, CORS) are
declared here as well via ``long_running_factories``; each factory is bound
to a Garage config so the bootstrap can compose a flat name→factory map
without the agent knowing each handler module by name.
"""

from __future__ import annotations

import logging

from stormpulse.commands.jobs import LongRunningFactory
from stormpulse.config import CommandDef, GarageConfig, ParamDef

logger = logging.getLogger(__name__)

# S3-strict bucket name (which Garage's bucket-create validator
# enforces): 3-63 chars, lowercase alphanumeric + hyphens, must start
# AND end alphanumeric. Garage CLI rejects names with leading
# underscores, uppercase, or any underscore at all on S3-strict
# deployments - see ``provision_bucket.py``'s throwaway-alias comment
# for the empirical lesson.
_BUCKET_NAME_PATTERN = r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]"
# Garage internal bucket UUID. The full form is 64 lowercase hex chars;
# the CLI displays a 16-char unique prefix and accepts either form as a
# bucket reference. The ``garage_state`` snapshot pushed to Storm carries
# the full 64-char form, so anywhere bucket_id rides as a parameter from
# the dashboard, it arrives at full length. Match both.
_BUCKET_ID_PATTERN = r"[a-f0-9]{16,64}"
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
            command=[
                docker,
                "exec",
                container,
                garage,
                "bucket",
                "info",
                "{bucket_name}",
            ],
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
            command=[
                docker,
                "exec",
                container,
                garage,
                "bucket",
                "create",
                "{bucket_name}",
            ],
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
            command=[
                docker,
                "exec",
                container,
                garage,
                "bucket",
                "delete",
                "--yes",
                "{bucket_name}",
            ],
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
        # The BUCKETS-006 Headroom wall, applied via the Garage admin HTTP API
        # (UpdateBucket), not the CLI: a typed call instead of scraping CLI text,
        # addressing the bucket by id (garage_bucket_id, never the local alias).
        # Handled by the JobManager, see garage/set_quota.py. The website's
        # recompute + provision dispatch this with bucket_id + max_size (decimal
        # bytes); without it, buckets silently carry no quota.
        "garage_bucket_set_quota": CommandDef(
            group="garage",
            command=["garage_bucket_set_quota"],  # internal - handled by JobManager
            timeout=30,  # single admin API call; long_running ignores this for duration
            description="Set the max-size Headroom quota on a bucket via the Garage admin API (BUCKETS-006)",
            long_running=True,
            params={
                "bucket_id": ParamDef(
                    placeholder="bucket_id",
                    default=None,
                    pattern=_BUCKET_ID_PATTERN,
                    description="Bucket id (garage_bucket_id), never the local alias",
                ),
                "max_size": ParamDef(
                    placeholder="max_size",
                    default=None,
                    pattern=r"[0-9]+",
                    description="Maximum size in bytes (decimal)",
                ),
            },
        ),
        "garage_set_account_key_create_bucket": CommandDef(
            group="garage",
            command=["garage_set_account_key_create_bucket"],  # internal - handled by JobManager
            timeout=30,  # single admin API call; long_running ignores this for duration
            description="Set or clear an account key's allow_create_bucket capability via the Garage admin API (BUCKETS-012 count backstop).",
            long_running=True,
            params={
                "access_key_id": ParamDef(
                    placeholder="access_key_id",
                    default=None,
                    pattern=_KEY_ID_PATTERN,
                    description="Account key id (GK...) to toggle",
                ),
                "enable": ParamDef(
                    placeholder="enable",
                    default=None,
                    pattern=r"(?:true|false)",
                    description="'true' to allow bucket creation, 'false' to deny",
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
            command=[
                docker,
                "exec",
                container,
                garage,
                "key",
                "delete",
                "--yes",
                "{key_id}",
            ],
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
            command=[
                docker,
                "exec",
                container,
                garage,
                "bucket",
                "allow",
                "--read",
                "--write",
                "--owner",
                "{bucket_name}",
                "--key",
                "{key_id}",
            ],
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
            command=[
                docker,
                "exec",
                container,
                garage,
                "bucket",
                "allow",
                "--read",
                "--write",
                "{bucket_name}",
                "--key",
                "{key_id}",
            ],
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
            command=[
                docker,
                "exec",
                container,
                garage,
                "bucket",
                "allow",
                "--read",
                "{bucket_name}",
                "--key",
                "{key_id}",
            ],
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
            command=[
                docker,
                "exec",
                container,
                garage,
                "bucket",
                "website",
                "--allow",
                "{bucket_name}",
                "--index-document",
                "{index_document}",
                "--error-document",
                "{error_document}",
            ],
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
            command=[
                docker,
                "exec",
                container,
                garage,
                "bucket",
                "website",
                "--deny",
                "{bucket_name}",
            ],
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
            command=[
                docker,
                "exec",
                container,
                garage,
                "bucket",
                "alias",
                "{bucket_name}",
                "{new_alias}",
            ],
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
            command=[
                docker,
                "exec",
                container,
                garage,
                "bucket",
                "unalias",
                "{alias_name}",
            ],
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
            command=[
                docker,
                "exec",
                container,
                garage,
                "bucket",
                "alias",
                "--local",
                "{key_id}",
                "{bucket_name}",
                "{new_alias}",
            ],
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
            command=[
                docker,
                "exec",
                container,
                garage,
                "bucket",
                "unalias",
                "--local",
                "{key_id}",
                "{alias_name}",
            ],
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
            command=["garage_refresh"],  # internal - not a subprocess
            timeout=30,
            description="Internal command - triggers immediate state collection and metrics push",
        ),
        "garage_provision_customer_bucket": CommandDef(
            group="garage",
            command=[
                "garage_provision_customer_bucket"
            ],  # internal - handled by JobManager
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
            command=[
                "garage_delete_provisioned_bucket"
            ],  # internal - handled by JobManager
            timeout=120,
            requires_confirmation=True,
            description="Orchestrated bucket deletion: detaches all aliases (using a temp global to bypass the orphan-rule deadlock when only locals exist), then deletes. Atomic with rollback.",
            long_running=True,
            params={
                "bucket_id": ParamDef(
                    placeholder="bucket_id",
                    default=None,
                    pattern=_BUCKET_ID_PATTERN,
                    description="Bucket UUID (16 or 64-char Garage ID) to delete",
                ),
            },
        ),
        "garage_provision_additional_key": CommandDef(
            group="garage",
            command=[
                "garage_provision_additional_key"
            ],  # internal - handled by JobManager
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
                    pattern=_BUCKET_ID_PATTERN,
                    description="Bucket UUID (16 or 64-char Garage ID) to attach the local alias to",
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
                    pattern=r"(?:all|rw|ro)",
                    description="Permission tier: 'rw'/'ro' add a tiered key to a bucket that already has an owner; 'all' mints the owner key onto an adopted bucket whose owner slot is free (claim-admin, BUCKETS-013).",
                ),
            },
        ),
        "garage_provision_account_key": CommandDef(
            group="garage",
            command=[
                "garage_provision_account_key"
            ],  # internal - handled by JobManager
            timeout=60,
            description="Orchestrated provisioning of an account key (key-level allow_create_bucket set) for customer aws cli / terraform bucket lifecycle. One step, no rollback - owns no bucket until the customer creates one over S3.",
            sensitive_output=True,  # the one-time secret rides in stdout/extras
            long_running=True,
            params={
                "new_key_name": ParamDef(
                    placeholder="new_key_name",
                    default=None,
                    pattern=_KEY_NAME_PATTERN,
                    description="Garage key name for the account key",
                ),
            },
        ),
        "garage_rotate_customer_key": CommandDef(
            group="garage",
            command=["garage_rotate_customer_key"],  # internal - handled by JobManager
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
                    pattern=_BUCKET_ID_PATTERN,
                    description="Bucket UUID (16 or 64-char Garage ID) the local alias is attached to",
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
        "garage_delete_key": CommandDef(
            group="garage",
            command=["garage_delete_key"],  # internal - handled by JobManager
            timeout=30,
            requires_confirmation=True,
            description=(
                "Admin-API key delete reporting a structured confirmed-gone "
                "outcome (deleted / already_absent), distinct from the legacy "
                "CLI garage_key_delete. Backs the credential-kill tombstone "
                "sweep (BUCKETS-013): a positive 404 is success, a transient "
                "error is not, so a still-live key is never certified dead."
            ),
            long_running=True,
            params={
                "key_id": ParamDef(
                    placeholder="key_id",
                    default=None,
                    pattern=_KEY_ID_PATTERN,
                    description="Garage key ID to delete",
                ),
            },
        ),
        "garage_detach_account_key": CommandDef(
            group="garage",
            command=["garage_detach_account_key"],  # internal - handled by JobManager
            timeout=30,
            requires_confirmation=True,
            description=(
                "Detach one account key's grant from a single bucket "
                "(BUCKETS-013): deny read/write/owner, drop the key's local "
                "alias, then read the key back and confirm the bucket is gone "
                "from its grant list. Grant-removal, not key-destruction: the "
                "key survives. Confirmed by the deny op's own result, never a 404."
            ),
            long_running=True,
            params={
                "bucket_id": ParamDef(
                    placeholder="bucket_id",
                    default=None,
                    pattern=_BUCKET_ID_PATTERN,
                    description="Bucket UUID (16 or 64-char Garage ID)",
                ),
                "account_key_id": ParamDef(
                    placeholder="account_key_id",
                    default=None,
                    pattern=_KEY_ID_PATTERN,
                    description="Account key Garage ID whose grant is removed",
                ),
                "local_alias": ParamDef(
                    placeholder="local_alias",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="The account key's local alias for the bucket to drop",
                ),
            },
        ),
        "garage_converge_account_key_rotation": CommandDef(
            group="garage",
            command=["garage_converge_account_key_rotation"],  # internal - JobManager
            timeout=120,
            description=(
                "One idempotent convergence pass of an account-key rotation "
                "(BUCKETS-013): grant the new key owner + alias on every bucket "
                "the old key owns that the new key does not, via the admin "
                "token. Re-dispatched each tick until it reports converged. "
                "Additive only (4a); the old key keeps its access until 4b."
            ),
            long_running=True,
            params={
                "old_key_id": ParamDef(
                    placeholder="old_key_id",
                    default=None,
                    pattern=_KEY_ID_PATTERN,
                    description="Account key Garage ID being rotated out",
                ),
                "new_key_id": ParamDef(
                    placeholder="new_key_id",
                    default=None,
                    pattern=_KEY_ID_PATTERN,
                    description="Replacement account key Garage ID receiving ownership",
                ),
            },
        ),
        "garage_bucket_clear": CommandDef(
            group="garage",
            command=[
                "garage_bucket_clear"
            ],  # internal - handled by JobManager, not a subprocess
            timeout=600,  # per-batch reference; long_running ignores it for total duration
            description=(
                "Bulk-delete every object in a bucket via the local Garage "
                "S3 endpoint. Two modes: customer-secret (bucket_name + "
                "credentials) or credential-less purge (bucket_id, no "
                "credentials; the agent self-mints a temporary key, ADR "
                "BUCKETS-010)"
            ),
            requires_confirmation=True,
            sensitive_output=True,  # the secret arrives in params; never log them
            long_running=True,
            params={
                "bucket_name": ParamDef(
                    placeholder="bucket_name",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="Bucket to clear (customer-secret mode)",
                ),
                "bucket_id": ParamDef(
                    placeholder="bucket_id",
                    default=None,
                    pattern=_BUCKET_ID_PATTERN,
                    description=(
                        "Bucket id (garage_bucket_id, never the local alias) "
                        "for the credential-less purge clear"
                    ),
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
        "garage_walk_bucket_stats": CommandDef(
            group="garage",
            command=["garage_walk_bucket_stats"],  # internal - handled by JobManager
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
                    # Empty (= root) or a non-control S3 key-prefix ending in '/'.
                    # This agent is the only charset gate; the website checks structure.
                    pattern=r"|[^/\x00-\x1f\x7f-\x9f][^\x00-\x1f\x7f-\x9f]*/",
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
            command=[
                docker,
                "exec",
                container,
                garage,
                "bucket",
                "deny",
                "--read",
                "--write",
                "--owner",
                "{bucket_name}",
                "--key",
                "{key_id}",
            ],
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


def long_running_factories(config: GarageConfig) -> dict[str, LongRunningFactory]:
    """Return the Garage long-running command name → handler-factory map.

    Each factory accepts the validated runtime params and returns a
    ``JobHandler`` coroutine. Imports its handler module lazily so the
    agent process doesn't load handler code for features that aren't
    installed on a given host.
    """
    from stormpulse.garage.clear_bucket import make_clear_bucket_handler
    from stormpulse.garage.delete_key import make_delete_key_handler
    from stormpulse.garage.detach_account_key import (
        make_detach_account_key_handler,
    )
    from stormpulse.garage.converge_account_key_rotation import (
        make_converge_account_key_rotation_handler,
    )
    from stormpulse.garage.delete_provisioned_bucket import (
        make_delete_provisioned_bucket_handler,
    )
    from stormpulse.garage.provision_additional_key import (
        make_provision_additional_key_handler,
    )
    from stormpulse.garage.provision_account_key import (
        make_provision_account_key_handler,
    )
    from stormpulse.garage.provision_bucket import (
        make_provision_customer_bucket_handler,
    )
    from stormpulse.garage.rotate_key import make_rotate_customer_key_handler
    from stormpulse.garage.set_account_key_capability import (
        make_set_account_key_capability_handler,
    )
    from stormpulse.garage.set_quota import make_set_quota_handler
    from stormpulse.garage.walk_bucket_stats import make_walk_bucket_stats_handler

    return {
        "garage_bucket_clear": (
            lambda params: make_clear_bucket_handler(config, params)
        ),
        "garage_bucket_set_quota": (
            lambda params: make_set_quota_handler(
                params, admin_url=config.admin_url, admin_token=config.admin_token,
            )
        ),
        "garage_set_account_key_create_bucket": (
            lambda params: make_set_account_key_capability_handler(
                params, admin_url=config.admin_url, admin_token=config.admin_token,
            )
        ),
        "garage_walk_bucket_stats": make_walk_bucket_stats_handler,
        "garage_provision_customer_bucket": (
            lambda params: make_provision_customer_bucket_handler(config, params)
        ),
        "garage_rotate_customer_key": (
            lambda params: make_rotate_customer_key_handler(config, params)
        ),
        "garage_provision_additional_key": (
            lambda params: make_provision_additional_key_handler(config, params)
        ),
        "garage_provision_account_key": (
            lambda params: make_provision_account_key_handler(config, params)
        ),
        "garage_delete_provisioned_bucket": (
            lambda params: make_delete_provisioned_bucket_handler(config, params)
        ),
        "garage_delete_key": (
            lambda params: make_delete_key_handler(config, params)
        ),
        "garage_detach_account_key": (
            lambda params: make_detach_account_key_handler(config, params)
        ),
        "garage_converge_account_key_rotation": (
            lambda params: make_converge_account_key_rotation_handler(config, params)
        ),
    }
