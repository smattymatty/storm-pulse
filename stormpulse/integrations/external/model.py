"""Frozen typed records and the failure taxonomy for the P1 external loader.

Pure data and errors. No I/O, no execution. Every record is
frozen and keyword-only so a caller cannot build a half-formed value positionally
or mutate one after construction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

# Canonical field-format contracts, shared across the subpackage so the parse
# path and the trust-crossing read paths validate identically.
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
INTEGRATION_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.-]+)?$")


class CapabilityRequest(StrEnum):
    """A capability a package may request in its manifest.

    Service management is NOT a capability: CORE-007 handles it structurally via
    the composition seal, not a granted token.
    """

    INTEGRATION_LOAD = "integration_load"
    COMMAND_CONTRIBUTOR = "command_contributor"


class TrustStatus(StrEnum):
    TRUSTED = "trusted"
    UNKNOWN = "unknown"
    REVOKED = "revoked"


class SignatureStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    UNVERIFIABLE = "unverifiable"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class FailureCode(StrEnum):
    """Terminal failure classes. The value is the stable code."""

    F1 = "F1"    # source missing / not a directory
    F2 = "F2"    # unsafe path / type / name
    F3 = "F3"    # traversal size / count limit
    F4 = "F4"    # manifest / signature schema
    F5 = "F5"    # unsupported key / algorithm
    F6 = "F6"    # digest / signature mismatch
    F7 = "F7"    # unknown / revoked publisher
    F8 = "F8"    # hostname confirmation
    F9 = "F9"    # trust-record conflict / corruption
    F10 = "F10"  # target digest path corrupt
    F11 = "F11"  # receipt corrupt / missing package
    F12 = "F12"  # lock unavailable
    F13 = "F13"  # disk full / permission / fsync
    F14 = "F14"  # source mutation race
    F15 = "F15"  # unexpected exception


class PackageError(Exception):
    """A terminal P1 failure carrying its taxonomy code.

    ``path`` is always package-relative, never a source-absolute path.
    """

    def __init__(self, code: FailureCode, message: str, *, path: str | None = None) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.message = message
        self.path = path


@dataclass(frozen=True, slots=True, kw_only=True)
class ManifestV1:
    integration_id: str
    version: str
    entry_module: str
    publisher_fingerprint: str
    schema_version: int = 1
    sdk_api: int = 1
    entry_object: str = "INTEGRATION"
    requested_capabilities: tuple[CapabilityRequest, ...] = ()
    command_specs_digest: str | None = None
    service_manifest_digest: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DetachedSignatureV1:
    publisher_fingerprint: str
    package_digest: str
    signature_b64: str
    schema_version: int = 1
    algorithm: str = "ed25519"


@dataclass(frozen=True, slots=True, kw_only=True)
class PublisherRecordV1:
    fingerprint: str
    public_key_b64: str
    label: str
    added_at: str
    schema_version: int = 1
    algorithm: str = "ed25519"
    revoked_at: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class InstallReceiptV1:
    agent_id: str
    integration_id: str
    version: str
    sdk_api: int
    package_digest: str
    manifest_digest: str
    publisher_fingerprint: str
    requested_capabilities: tuple[CapabilityRequest, ...]
    installed_relpath: str
    installed_at: str
    schema_version: int = 1
    command_specs_digest: str | None = None
    service_manifest_digest: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class SealedGrantV1:
    """The operator's execution authority for one installed package (CORE-007 D3).

    Distinct from an install receipt: a receipt attests bytes were installed and
    which key signed them; a grant authorizes RUNNING that code. The loader reads
    grants, never receipts. Binds every digest so any change invalidates the
    grant - authority never carries across versions. ``granted_capabilities`` is
    the whole set the package requested (grant is all-or-nothing); revocation is
    the capability-specific overlay (D3): ``revoked_capabilities`` fences those
    tokens while the rest keep their authority.
    """

    agent_id: str
    integration_id: str
    publisher_fingerprint: str
    package_digest: str
    manifest_digest: str
    granted_capabilities: tuple[CapabilityRequest, ...]
    sealed_at: str
    schema_version: int = 1
    seal_format_version: int = 1
    command_specs_digest: str | None = None
    service_manifest_digest: str | None = None
    revoked_capabilities: tuple[CapabilityRequest, ...] = ()
    revoked_at: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class Finding:
    code: str
    severity: Severity
    message: str
    integration_id: str | None = None
    package_digest: str | None = None
    path: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class InspectionReport:
    trust_status: TrustStatus
    signature_status: SignatureStatus
    file_count: int
    total_bytes: int
    manifest: ManifestV1 | None = None
    package_digest: str | None = None
    manifest_digest: str | None = None
    signature_fingerprint: str | None = None
    findings: tuple[Finding, ...] = ()
    executable_code_loaded: bool = False
