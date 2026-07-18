"""Validation patterns and param factories shared by the garage command specs.

Declaring a validated param in one place is the security win: a wrong-pattern
bucket name is unconstructable rather than a copy that drifted.
"""

from __future__ import annotations

from stormpulse.config import ParamDef

# S3-strict bucket name (which Garage's bucket-create validator
# enforces): 3-63 chars, lowercase alphanumeric + hyphens, must start
# AND end alphanumeric. Garage CLI rejects names with leading
# underscores, uppercase, or any underscore at all on S3-strict
# deployments - see ``provision_bucket.py``'s throwaway-alias comment
# for the empirical lesson.
BUCKET_NAME_PATTERN = r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]"
# Garage internal bucket UUID. The full form is 64 lowercase hex chars;
# the CLI displays a 16-char unique prefix and accepts either form as a
# bucket reference. The ``garage_state`` snapshot pushed to Storm carries
# the full 64-char form, so anywhere bucket_id rides as a parameter from
# the dashboard, it arrives at full length. Match both.
BUCKET_ID_PATTERN = r"[a-f0-9]{16,64}"
# Key names are not S3-bucket-shaped; Garage allows the broader set.
# Storm provisions keys as ``usr-<pk>-<bucket>-<tier>`` which uses
# hyphens only, but other ops paths may include underscores or mixed
# case for descriptive names.
KEY_NAME_PATTERN = r"[a-zA-Z0-9_][a-zA-Z0-9_-]*"
KEY_ID_PATTERN = r"[a-zA-Z0-9]+"
DOCUMENT_PATTERN = r"[a-zA-Z0-9._/-]+"


# ----- Param factories for the high-frequency, always-required shapes -----
# Each is declared once so its validation pattern can never drift across the
# ~13 (bucket_name) / 8 (key_id, bucket_id) / 4 (local_alias) sites that use
# it. The one-off params (key_name, aliases, tiers, credentials, ...) stay
# inline as ParamDef: there is no repetition to collapse, and an inline
# ParamDef shows its pattern and default at the spec site.


def bucket_name_param(description: str) -> ParamDef:
    """A required S3-strict bucket-name / global-alias param."""
    return ParamDef(
        placeholder="bucket_name",
        default=None,
        pattern=BUCKET_NAME_PATTERN,
        description=description,
    )


def key_id_param(description: str) -> ParamDef:
    """A required Garage access-key id param."""
    return ParamDef(
        placeholder="key_id",
        default=None,
        pattern=KEY_ID_PATTERN,
        description=description,
    )


def bucket_id_param(description: str) -> ParamDef:
    """A required Garage bucket UUID param (16 or 64 hex chars)."""
    return ParamDef(
        placeholder="bucket_id",
        default=None,
        pattern=BUCKET_ID_PATTERN,
        description=description,
    )


def local_alias_param(description: str) -> ParamDef:
    """A required local-alias param (bucket-name shaped, key-scoped)."""
    return ParamDef(
        placeholder="local_alias",
        default=None,
        pattern=BUCKET_NAME_PATTERN,
        description=description,
    )


def s3_credential_params(access_key_description: str) -> dict[str, ParamDef]:
    """The S3 endpoint + SigV4 credential quartet shared by clear and walk."""
    return {
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
            pattern=KEY_ID_PATTERN,
            description=access_key_description,
        ),
        "secret_access_key": ParamDef(
            placeholder="secret_access_key",
            default=None,
            pattern=r".+",
            description="Customer S3 secret. Held in agent process memory only for the job's lifetime.",
            secret=True,
        ),
    }
