"""Garage-specific whitelisted commands, as single-source CommandSpecs.

Most resolve to ``docker exec <container> /garage <subcommand>``, shell=False
(``mode="subprocess"``). The admin-API / S3 orchestrations are ``mode="job"``:
each carries its own lazy handler thunk, so there is no separate name->factory
map to drift against.

There is no ``garage_refresh`` here anymore: "refresh my state now" is a
generic, agent-owned capability synthesized for any Integration that declares
``collect_state`` (see ``stormpulse.agent.refresh``), so garage gets it for
free the same way a third-party integration would.

Two pieces of plumbing keep the whitelist scannable instead of a wall of
copy-paste: ``garage_cli(...)`` writes the ``docker exec <container> /garage``
prefix once, and the ``_bucket_name`` / ``_key_id`` / ``_bucket_id`` /
``_local_alias`` factories below declare the four high-frequency params once.
Declaring a validated param in one place is also the security win: a
wrong-pattern bucket name is unconstructable rather than a copy that drifted.
"""

from __future__ import annotations

import logging

from stormpulse.config import CommandSpec, ParamDef
from stormpulse.garage.config import GarageConfig

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


# ----- Param factories for the high-frequency, always-required shapes -----
# Each is declared once so its validation pattern can never drift across the
# ~13 (bucket_name) / 8 (key_id, bucket_id) / 4 (local_alias) sites that use
# it. The one-off params (key_name, aliases, tiers, credentials, ...) stay
# inline as ParamDef: there is no repetition to collapse, and an inline
# ParamDef shows its pattern and default at the spec site.


def _bucket_name(description: str) -> ParamDef:
    """A required S3-strict bucket-name / global-alias param."""
    return ParamDef(
        placeholder="bucket_name",
        default=None,
        pattern=_BUCKET_NAME_PATTERN,
        description=description,
    )


def _key_id(description: str) -> ParamDef:
    """A required Garage access-key id param."""
    return ParamDef(
        placeholder="key_id",
        default=None,
        pattern=_KEY_ID_PATTERN,
        description=description,
    )


def _bucket_id(description: str) -> ParamDef:
    """A required Garage bucket UUID param (16 or 64 hex chars)."""
    return ParamDef(
        placeholder="bucket_id",
        default=None,
        pattern=_BUCKET_ID_PATTERN,
        description=description,
    )


def _local_alias(description: str) -> ParamDef:
    """A required local-alias param (bucket-name shaped, key-scoped)."""
    return ParamDef(
        placeholder="local_alias",
        default=None,
        pattern=_BUCKET_NAME_PATTERN,
        description=description,
    )


def build_garage_specs(config: GarageConfig) -> dict[str, CommandSpec]:
    """Build the Garage command registry from config.

    Uses config.docker_binary, config.container_name, and config.garage_binary
    to construct the full subprocess command templates, and binds each job's
    handler thunk to ``config``.
    """
    docker = config.docker_binary
    container = config.container_name
    garage = config.garage_binary

    def garage_cli(*args: str) -> list[str]:
        """The ``docker exec <container> /garage ...`` prefix, written once.

        Subprocess specs pass only their garage subcommand and arguments;
        the docker/exec/container/binary plumbing lives here so each spec
        reads as the garage command it actually runs.
        """
        return [docker, "exec", container, garage, *args]

    # Lazy handler imports: loaded only when a live garage integration builds
    # its specs, so a garage-less host never imports handler code. Each thunk
    # fires at dispatch, when validated params exist.
    from stormpulse.garage.attach_account_key import (
        make_attach_account_key_handler,
    )
    from stormpulse.garage.clear_bucket import make_clear_bucket_handler
    from stormpulse.garage.converge_account_key_rotation import (
        make_converge_account_key_rotation_handler,
    )
    from stormpulse.garage.delete_key import make_delete_key_handler
    from stormpulse.garage.delete_provisioned_bucket import (
        make_delete_provisioned_bucket_handler,
    )
    from stormpulse.garage.detach_account_key import (
        make_detach_account_key_handler,
    )
    from stormpulse.garage.enforce_account_key_tier import (
        make_enforce_account_key_tier_handler,
    )
    from stormpulse.garage.get_bucket_owners import make_get_bucket_owners_handler
    from stormpulse.garage.get_key_buckets import make_get_key_buckets_handler
    from stormpulse.garage.provision_account_key import (
        make_provision_account_key_handler,
    )
    from stormpulse.garage.provision_additional_key import (
        make_provision_additional_key_handler,
    )
    from stormpulse.garage.provision_bucket import (
        make_provision_customer_bucket_handler,
    )
    from stormpulse.garage.rotate_key import make_rotate_customer_key_handler
    from stormpulse.garage.set_account_key_capability import (
        make_set_account_key_capability_handler,
    )
    from stormpulse.garage.set_quota import make_set_quota_handler
    from stormpulse.garage.snapshot_and_reap_account_key import (
        make_snapshot_and_reap_account_key_handler,
    )
    from stormpulse.garage.walk_bucket_stats import make_walk_bucket_stats_handler

    return {
        # ----- Read-only -----
        "garage_status": CommandSpec(
            group="garage",
            command=garage_cli("status"),
            timeout=15,
            description="Show Garage node status",
        ),
        "garage_stats": CommandSpec(
            group="garage",
            command=garage_cli("stats"),
            timeout=15,
            description="Show Garage cluster statistics",
        ),
        "garage_bucket_list": CommandSpec(
            group="garage",
            command=garage_cli("bucket", "list"),
            timeout=15,
            description="List all Garage buckets",
        ),
        "garage_bucket_info": CommandSpec(
            group="garage",
            command=garage_cli("bucket", "info", "{bucket_name}"),
            timeout=15,
            description="Show bucket details",
            params={"bucket_name": _bucket_name("Bucket name or alias")},
        ),
        "garage_key_list": CommandSpec(
            group="garage",
            command=garage_cli("key", "list"),
            timeout=15,
            description="List all Garage API keys",
        ),
        # ----- State-changing -----
        "garage_bucket_create": CommandSpec(
            group="garage",
            command=garage_cli("bucket", "create", "{bucket_name}"),
            timeout=15,
            description="Create a new bucket",
            params={"bucket_name": _bucket_name("Name for the new bucket")},
        ),
        "garage_bucket_delete": CommandSpec(
            group="garage",
            command=garage_cli("bucket", "delete", "--yes", "{bucket_name}"),
            timeout=15,
            requires_confirmation=True,
            description="Delete a bucket",
            params={"bucket_name": _bucket_name("Bucket to delete")},
        ),
        # The BUCKETS-006 Headroom wall, applied via the Garage admin HTTP API
        # (UpdateBucket), not the CLI: a typed call instead of scraping CLI text,
        # addressing the bucket by id (garage_bucket_id, never the local alias).
        # Handled by the JobManager, see garage/set_quota.py. The website's
        # recompute + provision dispatch this with bucket_id + max_size (decimal
        # bytes); without it, buckets silently carry no quota.
        "garage_bucket_set_quota": CommandSpec(
            group="garage",
            command=["garage_bucket_set_quota"],  # internal - handled by JobManager
            timeout=30,  # single admin API call; long_running ignores this for duration
            description="Set the max-size Headroom quota on a bucket via the Garage admin API (BUCKETS-006)",
            mode="job",
            # The recompute's OWN action: a post-success push would re-enter the
            # recompute and dispatch more set_quotas (a feedback loop), and a quota
            # change alters no customer-visible usage. No post-mutation refresh.
            self_reconciling=True,
            handler=lambda params: make_set_quota_handler(
                params, admin_url=config.admin_url, admin_token=config.admin_token,
            ),
            params={
                "bucket_id": _bucket_id(
                    "Bucket id (garage_bucket_id), never the local alias"
                ),
                "max_size": ParamDef(
                    placeholder="max_size",
                    default=None,
                    pattern=r"[0-9]+",
                    description="Maximum size in bytes (decimal)",
                ),
            },
        ),
        "garage_set_account_key_create_bucket": CommandSpec(
            group="garage",
            command=["garage_set_account_key_create_bucket"],  # internal - handled by JobManager
            timeout=30,  # single admin API call; long_running ignores this for duration
            description="Set or clear an account key's allow_create_bucket capability via the Garage admin API (BUCKETS-012 count backstop).",
            mode="job",
            handler=lambda params: make_set_account_key_capability_handler(
                params, admin_url=config.admin_url, admin_token=config.admin_token,
            ),
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
        "garage_key_create": CommandSpec(
            group="garage",
            command=garage_cli("key", "create", "{key_name}"),
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
        "garage_bucket_allow": CommandSpec(
            group="garage",
            command=garage_cli(
                "bucket", "allow", "--read", "--write", "--owner",
                "{bucket_name}", "--key", "{key_id}",
            ),
            timeout=15,
            description="Grant full access to a bucket for a key",
            params={
                "bucket_name": _bucket_name("Bucket to grant access to"),
                "key_id": _key_id("Key to grant access for"),
            },
        ),
        "garage_bucket_allow_rw": CommandSpec(
            group="garage",
            command=garage_cli(
                "bucket", "allow", "--read", "--write",
                "{bucket_name}", "--key", "{key_id}",
            ),
            timeout=15,
            description="Grant read-write access to a bucket for a key",
            params={
                "bucket_name": _bucket_name("Bucket to grant access to"),
                "key_id": _key_id("Key to grant access for"),
            },
        ),
        "garage_bucket_allow_ro": CommandSpec(
            group="garage",
            command=garage_cli(
                "bucket", "allow", "--read", "{bucket_name}", "--key", "{key_id}",
            ),
            timeout=15,
            description="Grant read-only access to a bucket for a key",
            params={
                "bucket_name": _bucket_name("Bucket to grant access to"),
                "key_id": _key_id("Key to grant access for"),
            },
        ),
        "garage_bucket_website_allow": CommandSpec(
            group="garage",
            command=garage_cli(
                "bucket", "website", "--allow", "{bucket_name}",
                "--index-document", "{index_document}",
                "--error-document", "{error_document}",
            ),
            timeout=30,
            description="Enable static website hosting on a bucket",
            params={
                "bucket_name": _bucket_name("Bucket name or alias"),
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
        "garage_bucket_website_deny": CommandSpec(
            group="garage",
            command=garage_cli("bucket", "website", "--deny", "{bucket_name}"),
            timeout=30,
            requires_confirmation=True,
            description="Disable static website hosting on a bucket",
            params={"bucket_name": _bucket_name("Bucket name or alias")},
        ),
        "garage_bucket_alias_global_add": CommandSpec(
            group="garage",
            command=garage_cli("bucket", "alias", "{bucket_name}", "{new_alias}"),
            timeout=15,
            description="Add a global alias to a bucket",
            params={
                "bucket_name": _bucket_name(
                    "Bucket reference: existing global alias or hex UUID"
                ),
                "new_alias": ParamDef(
                    placeholder="new_alias",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="New global alias to add",
                ),
            },
        ),
        "garage_bucket_alias_global_remove": CommandSpec(
            group="garage",
            command=garage_cli("bucket", "unalias", "{alias_name}"),
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
        "garage_bucket_alias_local_add": CommandSpec(
            group="garage",
            command=garage_cli(
                "bucket", "alias", "--local",
                "{key_id}", "{bucket_name}", "{new_alias}",
            ),
            timeout=15,
            description="Add a local alias scoped to an access key",
            params={
                "key_id": _key_id("Access key the local alias is scoped to"),
                "bucket_name": _bucket_name(
                    "Bucket reference: existing global alias or hex UUID"
                ),
                "new_alias": ParamDef(
                    placeholder="new_alias",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="New local alias to add",
                ),
            },
        ),
        "garage_bucket_alias_local_remove": CommandSpec(
            group="garage",
            command=garage_cli(
                "bucket", "unalias", "--local", "{key_id}", "{alias_name}",
            ),
            timeout=15,
            requires_confirmation=True,
            description="Remove a local alias scoped to an access key",
            params={
                "key_id": _key_id("Access key the local alias is scoped to"),
                "alias_name": ParamDef(
                    placeholder="alias_name",
                    default=None,
                    pattern=_BUCKET_NAME_PATTERN,
                    description="Local alias to remove",
                ),
            },
        ),
        "garage_provision_customer_bucket": CommandSpec(
            group="garage",
            command=["garage_provision_customer_bucket"],  # internal - handled by JobManager
            timeout=600,  # per-step reference; long_running ignores it for total duration
            description="Orchestrated bucket + admin key provisioning. Atomic with rollback. The rw/ro keys are added on demand via garage_provision_additional_key.",
            sensitive_output=True,  # secrets ride in stdout/extras
            mode="job",
            handler=lambda params: make_provision_customer_bucket_handler(config, params),
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
        "garage_delete_provisioned_bucket": CommandSpec(
            group="garage",
            command=["garage_delete_provisioned_bucket"],  # internal - handled by JobManager
            timeout=120,
            requires_confirmation=True,
            description="Orchestrated bucket deletion: detaches all aliases (using a temp global to bypass the orphan-rule deadlock when only locals exist), then deletes. Atomic with rollback.",
            mode="job",
            handler=lambda params: make_delete_provisioned_bucket_handler(config, params),
            params={
                "bucket_id": _bucket_id(
                    "Bucket UUID (16 or 64-char Garage ID) to delete"
                ),
            },
        ),
        "garage_provision_additional_key": CommandSpec(
            group="garage",
            command=["garage_provision_additional_key"],  # internal - handled by JobManager
            timeout=120,
            description="Orchestrated provisioning of an additional rw or ro key on an existing bucket. Atomic with rollback.",
            sensitive_output=True,  # the new secret rides in stdout/extras
            mode="job",
            handler=lambda params: make_provision_additional_key_handler(config, params),
            params={
                "new_key_name": ParamDef(
                    placeholder="new_key_name",
                    default=None,
                    pattern=_KEY_NAME_PATTERN,
                    description="Garage key name for the new tiered key",
                ),
                "bucket_id": _bucket_id(
                    "Bucket UUID (16 or 64-char Garage ID) to attach the local alias to"
                ),
                "local_alias": _local_alias(
                    "Local alias to attach on the new key (typically display_name)"
                ),
                "key_tier": ParamDef(
                    placeholder="key_tier",
                    default=None,
                    pattern=r"(?:all|rw|ro)",
                    description="Permission tier: 'rw'/'ro' add a tiered key to a bucket that already has an owner; 'all' mints the owner key onto an adopted bucket whose owner slot is free (claim-admin, BUCKETS-013).",
                ),
            },
        ),
        "garage_provision_account_key": CommandSpec(
            group="garage",
            command=["garage_provision_account_key"],  # internal - handled by JobManager
            timeout=60,
            description="Orchestrated provisioning of an account key for customer aws cli / terraform bucket lifecycle. The tier governs create capability (BUCKETS-016): an Admin key is minted with key-level allow_create_bucket, a Read-Write/Read-Only key with create disabled. One step, no rollback - owns no bucket until the customer creates one over S3.",
            sensitive_output=True,  # the one-time secret rides in stdout/extras
            mode="job",
            handler=lambda params: make_provision_account_key_handler(config, params),
            params={
                "new_key_name": ParamDef(
                    placeholder="new_key_name",
                    default=None,
                    pattern=_KEY_NAME_PATTERN,
                    description="Garage key name for the account key",
                ),
                "allow_create_bucket": ParamDef(
                    placeholder="allow_create_bucket",
                    default="false",
                    pattern=r"(?:true|false)",
                    description="BUCKETS-016 tier gate: 'true' mints an Admin key (key-level allow_create_bucket); 'false' mints a Read-Write/Read-Only key that cannot create buckets and reaches buckets only through attach. Defaults 'false' (FAIL CLOSED): a capability gate must never grant create on an absent signal. An Admin mint always sends 'true' explicitly; a mint that fails to send the flag yields a powerless key, not a root one.",
                ),
            },
        ),
        "garage_rotate_customer_key": CommandSpec(
            group="garage",
            command=["garage_rotate_customer_key"],  # internal - handled by JobManager
            timeout=120,
            description="Orchestrated key rotation: create new key, attach local alias, delete old key. Atomic with rollback.",
            sensitive_output=True,  # secrets ride in stdout/extras
            mode="job",
            handler=lambda params: make_rotate_customer_key_handler(config, params),
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
                "bucket_id": _bucket_id(
                    "Bucket UUID (16 or 64-char Garage ID) the local alias is attached to"
                ),
                "local_alias": _local_alias(
                    "Local alias to re-attach on the new key"
                ),
                "key_tier": ParamDef(
                    placeholder="key_tier",
                    default=None,
                    pattern=r"(?:all|rw|ro)",
                    description="Permission tier for the new key: 'all', 'rw', or 'ro'",
                ),
            },
        ),
        "garage_delete_key": CommandSpec(
            group="garage",
            command=["garage_delete_key"],  # internal - handled by JobManager
            timeout=30,
            requires_confirmation=True,
            description=(
                "Admin-API key delete reporting a structured confirmed-gone "
                "outcome (deleted / already_absent). Backs the credential-kill "
                "tombstone sweep (BUCKETS-013): a positive 404 is success, a "
                "transient error is not, so a still-live key is never certified "
                "dead."
            ),
            mode="job",
            handler=lambda params: make_delete_key_handler(config, params),
            params={
                "key_id": _key_id("Garage key ID to delete"),
            },
        ),
        "garage_detach_account_key": CommandSpec(
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
            mode="job",
            handler=lambda params: make_detach_account_key_handler(config, params),
            params={
                "bucket_id": _bucket_id("Bucket UUID (16 or 64-char Garage ID)"),
                "account_key_id": ParamDef(
                    placeholder="account_key_id",
                    default=None,
                    pattern=_KEY_ID_PATTERN,
                    description="Account key Garage ID whose grant is removed",
                ),
                "local_alias": _local_alias(
                    "The account key's local alias for the bucket to drop"
                ),
            },
        ),
        "garage_attach_account_key": CommandSpec(
            group="garage",
            command=["garage_attach_account_key"],  # internal - handled by JobManager
            timeout=30,
            requires_confirmation=True,
            description=(
                "Attach an account key to an existing bucket (BUCKETS-014): "
                "the inverse of detach. Grant the key the chosen tier "
                "(ro/rw/owner), add its local alias, then read the key back and "
                "confirm the grant landed. A deliberate, password-gated "
                "widening of a root credential, least-privilege by tier."
            ),
            mode="job",
            handler=lambda params: make_attach_account_key_handler(config, params),
            params={
                "bucket_id": _bucket_id("Bucket UUID (16 or 64-char Garage ID)"),
                "account_key_id": ParamDef(
                    placeholder="account_key_id",
                    default=None,
                    pattern=_KEY_ID_PATTERN,
                    description="Account key Garage ID receiving the grant",
                ),
                "local_alias": _local_alias(
                    "Local alias to attach on the key (the bucket's display_name)"
                ),
                "tier": ParamDef(
                    placeholder="tier",
                    default=None,
                    pattern=r"(?:ro|rw|owner)",
                    description="Grant tier: 'ro', 'rw', or 'owner' (least-privilege)",
                ),
            },
        ),
        "garage_enforce_account_key_tier": CommandSpec(
            group="garage",
            command=["garage_enforce_account_key_tier"],  # internal - JobManager
            timeout=120,
            description=(
                "Enforce that an account key's per-bucket grants never exceed "
                "its tier (BUCKETS-016): narrow every over-tier grant down to "
                "the tier via a precise set. All-or-nothing on stranding (abort "
                "if removing an owner grant would leave a bucket ownerless); "
                "idempotent when already enforced."
            ),
            mode="job",
            handler=lambda params: make_enforce_account_key_tier_handler(config, params),
            params={
                "account_key_id": ParamDef(
                    placeholder="account_key_id",
                    default=None,
                    pattern=_KEY_ID_PATTERN,
                    description="The account key (GK...) whose grants to enforce",
                ),
                "tier": ParamDef(
                    placeholder="tier",
                    default=None,
                    pattern=r"(?:all|rw|ro)",
                    description=(
                        "The tier ceiling (all/rw/ro); grants above it are "
                        "narrowed down"
                    ),
                ),
            },
        ),
        "garage_converge_account_key_rotation": CommandSpec(
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
            mode="job",
            # Re-dispatched each tick until it converges, so no single success is
            # the "did it land" moment; each pass's grant changes ride the periodic
            # walk already. No post-mutation refresh.
            self_reconciling=True,
            handler=lambda params: make_converge_account_key_rotation_handler(config, params),
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
                "bucket_snapshot": ParamDef(
                    placeholder="bucket_snapshot",
                    default=None,
                    pattern=None,
                    max_bytes=65536,
                    description=(
                        "Leak path only: JSON [{id, alias}] of the old key's "
                        "owned buckets captured before reap. When present, "
                        "converge from it instead of reading the dead old key."
                    ),
                ),
            },
        ),
        "garage_snapshot_and_reap_account_key": CommandSpec(
            group="garage",
            command=["garage_snapshot_and_reap_account_key"],  # internal - JobManager
            timeout=60,
            requires_confirmation=True,
            description=(
                "Leak-rotate kill (BUCKETS-013): snapshot the old key's owned "
                "buckets via GetKeyInfo, THEN delete the key object outright. "
                "Snapshot-before-kill so the list survives; deletes the object "
                "(not per-bucket deny) so a live key can't keep spawning "
                "buckets. The new key converges from the returned snapshot."
            ),
            mode="job",
            handler=lambda params: make_snapshot_and_reap_account_key_handler(config, params),
            params={
                "old_key_id": ParamDef(
                    placeholder="old_key_id",
                    default=None,
                    pattern=_KEY_ID_PATTERN,
                    description="Compromised account key Garage ID to snapshot then delete",
                ),
            },
        ),
        "garage_get_key_buckets": CommandSpec(
            group="garage",
            command=["garage_get_key_buckets"],  # internal - JobManager
            read_only=True,
            timeout=30,
            description=(
                "Read-only (BUCKETS-013): return the buckets an account key "
                "owns via GetKeyInfo. Storm does not store the key->bucket "
                "link, so the dashboard's per-key bucket list and revoke "
                "at-risk split come from this live read."
            ),
            mode="job",
            handler=lambda params: make_get_key_buckets_handler(config, params),
            params={
                "key_id": _key_id("Account key Garage ID to list owned buckets for"),
            },
        ),
        "garage_get_bucket_owners": CommandSpec(
            group="garage",
            command=["garage_get_bucket_owners"],  # internal - JobManager
            read_only=True,
            timeout=30,
            description=(
                "Read-only (BUCKETS-013): return the access keys that own a "
                "bucket via GetBucketInfo. Inverse of garage_get_key_buckets; "
                "Storm matches the ids to AccountKey rows for the bucket-detail "
                "provenance line."
            ),
            mode="job",
            handler=lambda params: make_get_bucket_owners_handler(config, params),
            params={
                "bucket_id": _bucket_id("Bucket UUID (16 or 64-char Garage ID)"),
            },
        ),
        "garage_bucket_clear": CommandSpec(
            group="garage",
            command=["garage_bucket_clear"],  # internal - handled by JobManager, not a subprocess
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
            mode="job",
            handler=lambda params: make_clear_bucket_handler(config, params),
            params={
                "bucket_name": _bucket_name("Bucket to clear (customer-secret mode)"),
                "bucket_id": _bucket_id(
                    "Bucket id (garage_bucket_id, never the local alias) "
                    "for the credential-less purge clear"
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
        "garage_walk_bucket_stats": CommandSpec(
            group="garage",
            command=["garage_walk_bucket_stats"],  # internal - handled by JobManager
            read_only=True,
            timeout=120,  # 100k objects ÷ 1000/page = 100 pages; ~1s/page p95
            description=(
                "Walk a bucket on the local Garage S3 endpoint to compute "
                "per-prefix object count + byte sum. Customer credentials "
                "in params; agent signs requests against loopback so the "
                "customer's home IP doesn't appear in Garage's access log."
            ),
            sensitive_output=True,  # the secret arrives in params
            mode="job",
            handler=make_walk_bucket_stats_handler,
            params={
                "bucket_name": _bucket_name("Bucket to walk (local alias = display_name)"),
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
        "garage_bucket_deny": CommandSpec(
            group="garage",
            command=garage_cli(
                "bucket", "deny", "--read", "--write", "--owner",
                "{bucket_name}", "--key", "{key_id}",
            ),
            timeout=15,
            requires_confirmation=True,
            description="Revoke all access to a bucket for a key",
            params={
                "bucket_name": _bucket_name("Bucket to revoke access from"),
                "key_id": _key_id("Key to revoke access for"),
            },
        ),
    }
