"""State-changing Garage CLI passthroughs: single ``docker exec`` commands
with validated params, no orchestration."""

from __future__ import annotations

from typing import Callable

from stormpulse.config import CommandSpec, ParamDef
from stormpulse.garage.commands.params import (
    BUCKET_NAME_PATTERN,
    DOCUMENT_PATTERN,
    KEY_NAME_PATTERN,
    bucket_name_param,
    key_id_param,
)


def build_raw_specs(
    garage_cli: Callable[..., list[str]],
) -> dict[str, CommandSpec]:
    """Build the state-changing CLI passthrough specs on the shared prefix."""
    return {
        "garage_bucket_create": CommandSpec(
            group="garage",
            command=garage_cli("bucket", "create", "{bucket_name}"),
            timeout=15,
            description="Create a new bucket",
            params={"bucket_name": bucket_name_param("Name for the new bucket")},
        ),
        "garage_bucket_delete": CommandSpec(
            group="garage",
            command=garage_cli("bucket", "delete", "--yes", "{bucket_name}"),
            timeout=15,
            requires_confirmation=True,
            description="Delete a bucket",
            params={"bucket_name": bucket_name_param("Bucket to delete")},
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
                    pattern=KEY_NAME_PATTERN,
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
                "bucket_name": bucket_name_param("Bucket to grant access to"),
                "key_id": key_id_param("Key to grant access for"),
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
                "bucket_name": bucket_name_param("Bucket to grant access to"),
                "key_id": key_id_param("Key to grant access for"),
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
                "bucket_name": bucket_name_param("Bucket to grant access to"),
                "key_id": key_id_param("Key to grant access for"),
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
                "bucket_name": bucket_name_param("Bucket name or alias"),
                "index_document": ParamDef(
                    placeholder="index_document",
                    default="index.html",
                    pattern=DOCUMENT_PATTERN,
                    description="Index document filename",
                ),
                "error_document": ParamDef(
                    placeholder="error_document",
                    default="404.html",
                    pattern=DOCUMENT_PATTERN,
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
            params={"bucket_name": bucket_name_param("Bucket name or alias")},
        ),
        "garage_bucket_alias_global_add": CommandSpec(
            group="garage",
            command=garage_cli("bucket", "alias", "{bucket_name}", "{new_alias}"),
            timeout=15,
            description="Add a global alias to a bucket",
            params={
                "bucket_name": bucket_name_param(
                    "Bucket reference: existing global alias or hex UUID"
                ),
                "new_alias": ParamDef(
                    placeholder="new_alias",
                    default=None,
                    pattern=BUCKET_NAME_PATTERN,
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
                    pattern=BUCKET_NAME_PATTERN,
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
                "key_id": key_id_param("Access key the local alias is scoped to"),
                "bucket_name": bucket_name_param(
                    "Bucket reference: existing global alias or hex UUID"
                ),
                "new_alias": ParamDef(
                    placeholder="new_alias",
                    default=None,
                    pattern=BUCKET_NAME_PATTERN,
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
                "key_id": key_id_param("Access key the local alias is scoped to"),
                "alias_name": ParamDef(
                    placeholder="alias_name",
                    default=None,
                    pattern=BUCKET_NAME_PATTERN,
                    description="Local alias to remove",
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
                "bucket_name": bucket_name_param("Bucket to revoke access from"),
                "key_id": key_id_param("Key to revoke access for"),
            },
        ),
    }
