"""Declarative inspection.

Reports a package's identity, digest, and trust status without importing or
executing any of its code. Structural problems (unreadable tree, unparseable
manifest/signature) raise; trust problems (unknown or revoked publisher, a
signature that does not verify) are reported in the returned status and findings,
not raised, so ``inspect`` on a structurally-valid package always yields a report.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from stormpulse.integrations.external import digest, manifest, trust
from stormpulse.integrations.external.model import (
    FailureCode,
    Finding,
    InspectionReport,
    PackageError,
    Severity,
    SignatureStatus,
    TrustStatus,
)


def inspect_package(source: Path, state_dir: Path) -> InspectionReport:
    scan = digest.scan_and_hash(source)
    if scan.manifest_bytes is None:
        raise PackageError(FailureCode.F4, "package has no manifest")
    if scan.signature_bytes is None:
        raise PackageError(FailureCode.F4, "package has no signature")
    parsed = manifest.parse_manifest(scan.manifest_bytes)
    signature = manifest.parse_signature(scan.signature_bytes)
    manifest_digest = "sha256:" + hashlib.sha256(scan.manifest_bytes).hexdigest()

    findings: list[Finding] = []
    record = trust.lookup(state_dir, parsed.publisher_fingerprint)
    if record is None:
        trust_status = TrustStatus.UNKNOWN
    elif trust.is_active(record):
        trust_status = TrustStatus.TRUSTED
    else:
        trust_status = TrustStatus.REVOKED

    # trust_status reflects the *manifest's* fingerprint while signature_fingerprint
    # reports the *signature's*; when they disagree the report can show a trusted
    # publisher next to a signature from a different key. That is safe only because
    # signature_status is INVALID here. This branch reports-and-continues rather than
    # asserting, so a structurally-valid package always yields a report.
    if signature.publisher_fingerprint != parsed.publisher_fingerprint:
        findings.append(_error("publisher_mismatch", "signature and manifest name different publishers"))
        signature_status = SignatureStatus.INVALID
    elif signature.package_digest != scan.package_digest:
        findings.append(_error("digest_mismatch", "signature is for a different package digest"))
        signature_status = SignatureStatus.INVALID
    elif record is None:
        signature_status = SignatureStatus.UNVERIFIABLE
    elif trust.verify_signature(
        record,
        signature,
        package_digest=scan.package_digest,
        integration_id=parsed.integration_id,
        version=parsed.version,
    ):
        signature_status = SignatureStatus.VALID
    else:
        findings.append(_error("signature_invalid", "signature does not verify against the approved key"))
        signature_status = SignatureStatus.INVALID

    if trust_status is TrustStatus.UNKNOWN:
        findings.append(_warn("publisher_unknown", "publisher is not approved on this agent"))
    elif trust_status is TrustStatus.REVOKED:
        findings.append(_warn("publisher_revoked", "publisher has been revoked on this agent"))

    return InspectionReport(
        manifest=parsed,
        package_digest=scan.package_digest,
        manifest_digest=manifest_digest,
        signature_fingerprint=signature.publisher_fingerprint,
        trust_status=trust_status,
        signature_status=signature_status,
        file_count=scan.file_count,
        total_bytes=scan.total_bytes,
        findings=tuple(sorted(findings, key=lambda f: (_SEVERITY_RANK[f.severity], f.code, f.path or ""))),
        executable_code_loaded=False,
    )


_SEVERITY_RANK = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}


def _error(code: str, message: str) -> Finding:
    return Finding(code=code, severity=Severity.ERROR, message=message)


def _warn(code: str, message: str) -> Finding:
    return Finding(code=code, severity=Severity.WARNING, message=message)
