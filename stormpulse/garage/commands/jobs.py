"""Orchestrated ``mode="job"`` specs: admin-API / S3 jobs carrying lazy
handler thunks, including the read-only live reads (get/walk)."""

from __future__ import annotations

from stormpulse.config import CommandSpec, ParamDef
from stormpulse.garage.commands.params import (
    BUCKET_NAME_PATTERN,
    KEY_ID_PATTERN,
    KEY_NAME_PATTERN,
    bucket_id_param,
    bucket_name_param,
    key_id_param,
    local_alias_param,
    s3_credential_params,
)
from stormpulse.garage.config import GarageConfig
from stormpulse.garage.tiers import ATTACH_TIER_PATTERN, TIER_PATTERN


def build_job_specs(config: GarageConfig) -> dict[str, CommandSpec]:
    """Build the orchestrated job specs, binding each handler thunk to config."""
    # Lazy handler imports: loaded only when a live garage integration builds
    # its specs, so a garage-less host never imports handler code. Each thunk
    # fires at dispatch, when validated params exist.
    from stormpulse.garage.jobs.attach_account_key import (
        make_attach_account_key_handler,
    )
    from stormpulse.garage.jobs.clear_bucket import make_clear_bucket_handler
    from stormpulse.garage.jobs.converge_account_key_rotation import (
        make_converge_account_key_rotation_handler,
    )
    from stormpulse.garage.jobs.delete_customer_key import (
        make_delete_customer_key_handler,
    )
    from stormpulse.garage.jobs.delete_key import make_delete_key_handler
    from stormpulse.garage.jobs.delete_provisioned_bucket import (
        make_delete_provisioned_bucket_handler,
    )
    from stormpulse.garage.jobs.detach_account_key import (
        make_detach_account_key_handler,
    )
    from stormpulse.garage.jobs.enforce_account_key_tier import (
        make_enforce_account_key_tier_handler,
    )
    from stormpulse.garage.jobs.get_bucket_owners import make_get_bucket_owners_handler
    from stormpulse.garage.jobs.get_key_buckets import make_get_key_buckets_handler
    from stormpulse.garage.jobs.provision_account_key import (
        make_provision_account_key_handler,
    )
    from stormpulse.garage.jobs.provision_additional_key import (
        make_provision_additional_key_handler,
    )
    from stormpulse.garage.jobs.provision_bucket import (
        make_provision_customer_bucket_handler,
    )
    from stormpulse.garage.jobs.rotate_key import make_rotate_customer_key_handler
    from stormpulse.garage.jobs.set_account_key_capability import (
        make_set_account_key_capability_handler,
    )
    from stormpulse.garage.jobs.set_quota import make_set_quota_handler
    from stormpulse.garage.jobs.snapshot_and_reap_account_key import (
        make_snapshot_and_reap_account_key_handler,
    )
    from stormpulse.garage.jobs.walk_bucket_stats import make_walk_bucket_stats_handler

    return {
        # The Headroom wall, applied via the Garage admin HTTP API
        # (UpdateBucket), not the CLI: a typed call instead of scraping CLI text,
        # addressing the bucket by id (garage_bucket_id, never the local alias).
        # Handled by the JobManager, see garage/set_quota.py. The website's
        # recompute + provision dispatch this with bucket_id + max_size (decimal
        # bytes); without it, buckets silently carry no quota.
        "garage_bucket_set_quota": CommandSpec(
            group="garage",
            command=["garage_bucket_set_quota"],  # internal - handled by JobManager
            timeout=30,  # single admin API call; long_running ignores this for duration
            description="Set the max-size Headroom quota on a bucket via the Garage admin API",
            mode="job",
            # The recompute's OWN action: a post-success push would re-enter the
            # recompute and dispatch more set_quotas (a feedback loop), and a quota
            # change alters no customer-visible usage. No post-mutation refresh.
            self_reconciling=True,
            handler=lambda params: make_set_quota_handler(
                params, admin_url=config.admin_url, admin_token=config.admin_token,
            ),
            params={
                "bucket_id": bucket_id_param(
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
            description="Set or clear an account key's allow_create_bucket capability via the Garage admin API.",
            mode="job",
            handler=lambda params: make_set_account_key_capability_handler(
                params, admin_url=config.admin_url, admin_token=config.admin_token,
            ),
            params={
                "access_key_id": ParamDef(
                    placeholder="access_key_id",
                    default=None,
                    pattern=KEY_ID_PATTERN,
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
                    pattern=BUCKET_NAME_PATTERN,
                    description="Customer-facing bucket name; becomes the local alias on the admin key",
                ),
                "key_name_admin": ParamDef(
                    placeholder="key_name_admin",
                    default=None,
                    pattern=KEY_NAME_PATTERN,
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
                "bucket_id": bucket_id_param(
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
                    pattern=KEY_NAME_PATTERN,
                    description="Garage key name for the new tiered key",
                ),
                "bucket_id": bucket_id_param(
                    "Bucket UUID (16 or 64-char Garage ID) to attach the local alias to"
                ),
                "local_alias": local_alias_param(
                    "Local alias to attach on the new key (typically display_name)"
                ),
                "key_tier": ParamDef(
                    placeholder="key_tier",
                    default=None,
                    pattern=TIER_PATTERN,
                    description="Permission tier: 'rw'/'ro' add a tiered key to a bucket that already has an owner; 'all' mints the owner key onto an adopted bucket whose owner slot is free (claim-admin).",
                ),
            },
        ),
        "garage_provision_account_key": CommandSpec(
            group="garage",
            command=["garage_provision_account_key"],  # internal - handled by JobManager
            timeout=60,
            description="Orchestrated provisioning of an account key for customer aws cli / terraform bucket lifecycle. The tier governs create capability: an Admin key is minted with key-level allow_create_bucket, a Read-Write/Read-Only key with create disabled. One step, no rollback - owns no bucket until the customer creates one over S3.",
            sensitive_output=True,  # the one-time secret rides in stdout/extras
            mode="job",
            handler=lambda params: make_provision_account_key_handler(config, params),
            params={
                "new_key_name": ParamDef(
                    placeholder="new_key_name",
                    default=None,
                    pattern=KEY_NAME_PATTERN,
                    description="Garage key name for the account key",
                ),
                "allow_create_bucket": ParamDef(
                    placeholder="allow_create_bucket",
                    default="false",
                    pattern=r"(?:true|false)",
                    description="tier gate: 'true' mints an Admin key (key-level allow_create_bucket); 'false' mints a Read-Write/Read-Only key that cannot create buckets and reaches buckets only through attach. Defaults 'false' (FAIL CLOSED): a capability gate must never grant create on an absent signal. An Admin mint always sends 'true' explicitly; a mint that fails to send the flag yields a powerless key, not a root one.",
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
                    pattern=KEY_ID_PATTERN,
                    description="Garage ID of the key being rotated out",
                ),
                "new_key_name": ParamDef(
                    placeholder="new_key_name",
                    default=None,
                    pattern=KEY_NAME_PATTERN,
                    description="Name for the replacement key",
                ),
                "bucket_id": bucket_id_param(
                    "Bucket UUID (16 or 64-char Garage ID) the local alias is attached to"
                ),
                "local_alias": local_alias_param(
                    "Local alias to re-attach on the new key"
                ),
                "key_tier": ParamDef(
                    placeholder="key_tier",
                    default=None,
                    pattern=TIER_PATTERN,
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
                "tombstone sweep: a positive 404 is success, a "
                "transient error is not, so a still-live key is never certified "
                "dead."
            ),
            mode="job",
            handler=lambda params: make_delete_key_handler(config, params),
            params={
                "key_id": key_id_param("Garage key ID to delete"),
            },
        ),
        "garage_delete_customer_key": CommandSpec(
            group="garage",
            command=["garage_delete_customer_key"],  # internal - handled by JobManager
            timeout=30,
            requires_confirmation=True,
            description=(
                "Guarded admin-API delete of a per-bucket customer key: "
                "verifies one of the covering keys still holds a live grant "
                "on the bucket (GetBucketInfo, live state), then deletes with "
                "the same confirmed-gone semantics as garage_delete_key. "
                "All-or-nothing: a failed coverage check (not_covered) makes "
                "no changes."
            ),
            mode="job",
            handler=lambda params: make_delete_customer_key_handler(config, params),
            params={
                "key_id": key_id_param("Garage key ID to delete"),
                "bucket_id": bucket_id_param(
                    "Bucket UUID whose remaining coverage gates the delete"
                ),
                "covering_key_ids": ParamDef(
                    placeholder="covering_key_ids",
                    default=None,
                    pattern=r"[a-zA-Z0-9]+(,[a-zA-Z0-9]+)*",
                    description=(
                        "Comma-separated Garage key ids that count as "
                        "coverage; at least one must hold a live grant"
                    ),
                ),
            },
        ),
        "garage_detach_account_key": CommandSpec(
            group="garage",
            command=["garage_detach_account_key"],  # internal - handled by JobManager
            timeout=30,
            requires_confirmation=True,
            description=(
                "Detach one account key's grant from a single bucket "
                ": deny read/write/owner, drop the key's local "
                "alias, then read the key back and confirm the bucket is gone "
                "from its grant list. Grant-removal, not key-destruction: the "
                "key survives. Confirmed by the deny op's own result, never a 404."
            ),
            mode="job",
            handler=lambda params: make_detach_account_key_handler(config, params),
            params={
                "bucket_id": bucket_id_param("Bucket UUID (16 or 64-char Garage ID)"),
                "account_key_id": ParamDef(
                    placeholder="account_key_id",
                    default=None,
                    pattern=KEY_ID_PATTERN,
                    description="Account key Garage ID whose grant is removed",
                ),
                "local_alias": local_alias_param(
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
                "Attach an account key to an existing bucket: "
                "the inverse of detach. Grant the key the chosen tier "
                "(ro/rw/owner), add its local alias, then read the key back and "
                "confirm the grant landed. A deliberate, password-gated "
                "widening of a root credential, least-privilege by tier."
            ),
            mode="job",
            handler=lambda params: make_attach_account_key_handler(config, params),
            params={
                "bucket_id": bucket_id_param("Bucket UUID (16 or 64-char Garage ID)"),
                "account_key_id": ParamDef(
                    placeholder="account_key_id",
                    default=None,
                    pattern=KEY_ID_PATTERN,
                    description="Account key Garage ID receiving the grant",
                ),
                "local_alias": local_alias_param(
                    "Local alias to attach on the key (the bucket's display_name)"
                ),
                "tier": ParamDef(
                    placeholder="tier",
                    default=None,
                    pattern=ATTACH_TIER_PATTERN,
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
                "its tier: narrow every over-tier grant down to "
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
                    pattern=KEY_ID_PATTERN,
                    description="The account key (GK...) whose grants to enforce",
                ),
                "tier": ParamDef(
                    placeholder="tier",
                    default=None,
                    pattern=TIER_PATTERN,
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
                ": grant the new key owner + alias on every bucket "
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
                    pattern=KEY_ID_PATTERN,
                    description="Account key Garage ID being rotated out",
                ),
                "new_key_id": ParamDef(
                    placeholder="new_key_id",
                    default=None,
                    pattern=KEY_ID_PATTERN,
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
                "Leak-rotate kill: snapshot the old key's owned "
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
                    pattern=KEY_ID_PATTERN,
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
                "Read-only: return the buckets an account key "
                "owns via GetKeyInfo. Storm does not store the key->bucket "
                "link, so the dashboard's per-key bucket list and revoke "
                "at-risk split come from this live read."
            ),
            mode="job",
            handler=lambda params: make_get_key_buckets_handler(config, params),
            params={
                "key_id": key_id_param("Account key Garage ID to list owned buckets for"),
            },
        ),
        "garage_get_bucket_owners": CommandSpec(
            group="garage",
            command=["garage_get_bucket_owners"],  # internal - JobManager
            read_only=True,
            timeout=30,
            description=(
                "Read-only: return the access keys that own a "
                "bucket via GetBucketInfo. Inverse of garage_get_key_buckets; "
                "Storm matches the ids to AccountKey rows for the bucket-detail "
                "provenance line."
            ),
            mode="job",
            handler=lambda params: make_get_bucket_owners_handler(config, params),
            params={
                "bucket_id": bucket_id_param("Bucket UUID (16 or 64-char Garage ID)"),
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
                "credentials; the agent self-mints a temporary key)"
            ),
            requires_confirmation=True,
            sensitive_output=True,  # the secret arrives in params; never log them
            mode="job",
            handler=lambda params: make_clear_bucket_handler(config, params),
            params={
                "bucket_name": bucket_name_param("Bucket to clear (customer-secret mode)"),
                "bucket_id": bucket_id_param(
                    "Bucket id (garage_bucket_id, never the local alias) "
                    "for the credential-less purge clear"
                ),
                **s3_credential_params("Customer S3 access key ID"),
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
                "bucket_name": bucket_name_param("Bucket to walk (local alias = display_name)"),
                **s3_credential_params("Customer S3 access key ID (any tier)"),
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
    }
