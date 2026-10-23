"""Diagnostics over the installed state.

Reports drift, corruption, revocation, and orphans as deterministic findings. It
never imports a package and never repairs. It holds the state lock so it cannot
observe a half-committed install, and it re-verifies the one file the digest
cannot protect: the detached signature is excluded from the package digest, so
doctor re-parses and re-verifies it, and cross-checks the receipt against the
installed manifest, to enforce that local state still corresponds. Every entry in
the state tree receives a status; nothing is silently skipped.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import re
from pathlib import Path

from stormpulse.integrations.external import (
    digest,
    grants,
    layout,
    ledger,
    manifest,
    trust,
)
from stormpulse.integrations.external.model import (
    Finding,
    InstallReceiptV1,
    PackageError,
    Severity,
)

_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def doctor_packages(
    state_dir: Path, integration_id: str | None = None
) -> list[Finding]:
    with layout.state_lock(state_dir):  # lock so we never observe a half-commit
        findings: list[Finding] = []
        referenced: set[str] = set()
        packages_root = layout.packages_dir(state_dir)
        _check_receipts(state_dir, packages_root, integration_id, referenced, findings)
        if integration_id is None:
            _check_orphans(state_dir, packages_root, referenced, findings)
        findings.sort(
            key=lambda f: (
                f.integration_id or "",
                f.package_digest or "",
                f.code,
                f.path or "",
            )
        )
        return findings


def _check_receipts(
    state_dir: Path,
    packages_root: Path,
    integration_id: str | None,
    referenced: set[str],
    findings: list[Finding],
) -> None:
    root = layout.receipts_dir(state_dir)
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            findings.append(_stray(entry.name, "unexpected file under receipts/"))
    # receipts/<id>/<entry>: one pass over every receipt folder's entries.
    for path in sorted(root.glob("*/*")):
        owner = path.parent.name
        if integration_id is not None and owner != integration_id:
            continue
        if not (path.is_file() and path.suffix == ".json"):
            findings.append(
                _stray(f"{owner}/{path.name}", "unexpected entry in a receipt folder")
            )
            continue
        before = len(findings)
        _check_receipt(state_dir, packages_root, path, referenced, findings)
        _demote_if_superseded(state_dir, owner, path.stem, findings, before)


def _check_orphans(
    state_dir: Path, packages_root: Path, referenced: set[str], findings: list[Finding]
) -> None:
    for entry in sorted(packages_root.iterdir()):
        if not entry.is_dir():
            findings.append(_stray(entry.name, "unexpected file under packages/"))
            continue
        if entry.name not in referenced:
            package_digest = (
                "sha256:" + entry.name if _HEX_RE.match(entry.name) else None
            )
            findings.append(
                Finding(
                    code="orphan_package",
                    severity=Severity.WARNING,
                    package_digest=package_digest,
                    path=entry.name,
                    message="package tree has no receipt",
                )
            )
    for leftover in sorted(layout.tmp_dir(state_dir).iterdir()):
        findings.append(
            Finding(
                code="orphan_temp",
                severity=Severity.WARNING,
                path=leftover.name,
                message="leftover staging from an interrupted install",
            )
        )


def _demote_if_superseded(
    state_dir: Path,
    integration_id: str,
    hex_part: str,
    findings: list[Finding],
    start: int,
) -> None:
    """Nothing loads a package the active pointer has moved past, so its errors are
    warnings: they still say a rollback to it would fail (staging, 2026-10-23)."""
    active = grants.active_grant(state_dir, integration_id)
    if active is None or active.package_digest.split(":", 1)[1] == hex_part:
        return
    for i in range(start, len(findings)):
        if findings[i].severity is Severity.ERROR:
            findings[i] = dataclasses.replace(
                findings[i],
                severity=Severity.WARNING,
                message=f"{findings[i].message} (superseded package: a rollback to it would fail)",
            )


def _check_receipt(
    state_dir: Path,
    packages_root: Path,
    path: Path,
    referenced: set[str],
    findings: list[Finding],
) -> None:
    try:
        receipt = ledger.read_receipt(path)
    except PackageError as exc:
        referenced.add(
            path.stem
        )  # the filename is the digest hex; avoid a spurious orphan too
        findings.append(_error("receipt_corrupt", exc.message, path=path.name))
        return

    hex_part = receipt.package_digest.split(":", 1)[
        1
    ]  # already DIGEST_RE-validated by read_receipt
    referenced.add(hex_part)
    target = packages_root / hex_part
    if not target.is_dir():
        findings.append(
            _error(
                "missing_package",
                "receipt references a missing package tree",
                receipt=receipt,
            )
        )
        return

    try:
        scan = digest.scan_and_hash(target)
    except PackageError as exc:
        findings.append(_error("package_corrupt", exc.message, receipt=receipt))
        return
    if scan.package_digest != receipt.package_digest:
        findings.append(
            _error(
                "package_corrupt",
                "installed tree does not match its receipt digest",
                receipt=receipt,
            )
        )
        return
    if scan.manifest_bytes is None or scan.signature_bytes is None:
        findings.append(
            _error(
                "package_corrupt",
                "installed tree is missing its manifest or signature",
                receipt=receipt,
            )
        )
        return

    try:
        installed_manifest = manifest.parse_manifest(scan.manifest_bytes)
        signature = manifest.parse_signature(scan.signature_bytes)
    except PackageError as exc:
        findings.append(_error("package_corrupt", exc.message, receipt=receipt))
        return

    manifest_digest = "sha256:" + hashlib.sha256(scan.manifest_bytes).hexdigest()
    if (
        installed_manifest.integration_id != receipt.integration_id
        or installed_manifest.version != receipt.version
        or installed_manifest.publisher_fingerprint != receipt.publisher_fingerprint
        or manifest_digest != receipt.manifest_digest
    ):
        findings.append(
            _error(
                "receipt_mismatch",
                "receipt does not match the installed manifest",
                receipt=receipt,
            )
        )
        return

    # Content is intact; a writable file or directory is mode drift (a crash before
    # the root chmod, or a hand-edit). The digest ignores modes, so doctor is the
    # only place this is ever visible. Reinstall re-seals it, so it is a warning.
    if _has_writable_entry(target):
        findings.append(
            _warn(
                "mode_drift",
                "installed tree has writable files or directories",
                receipt=receipt,
            )
        )

    try:
        record = trust.lookup(state_dir, installed_manifest.publisher_fingerprint)
    except PackageError as exc:
        findings.append(_warn("publisher_record_corrupt", exc.message, receipt=receipt))
        return
    if record is None:
        # A vanished publisher record is stranger than a revoked one: warn, don't skip.
        findings.append(
            _warn(
                "publisher_unknown",
                "installed package's publisher is not in the trust store",
                receipt=receipt,
            )
        )
    elif not trust.is_active(record):
        findings.append(
            _warn(
                "publisher_revoked",
                "installed package was signed by a now-revoked publisher",
                receipt=receipt,
            )
        )

    if record is not None and not trust.verify_signature(
        record,
        signature,
        package_digest=scan.package_digest,
        integration_id=installed_manifest.integration_id,
        version=installed_manifest.version,
    ):
        findings.append(
            _error(
                "signature_invalid",
                "installed signature does not verify against the approved key",
                receipt=receipt,
            )
        )


def _has_writable_entry(target: Path) -> bool:
    for dirpath, _dirnames, filenames in os.walk(target):
        if os.stat(dirpath).st_mode & 0o222:
            return True
        for name in filenames:
            if os.stat(os.path.join(dirpath, name)).st_mode & 0o222:
                return True
    return False


def _stray(path: str, message: str) -> Finding:
    return Finding(
        code="stray_file", severity=Severity.WARNING, path=path, message=message
    )


def _error(
    code: str,
    message: str,
    *,
    path: str | None = None,
    receipt: InstallReceiptV1 | None = None,
) -> Finding:
    return _finding(code, Severity.ERROR, message, path=path, receipt=receipt)


def _warn(
    code: str,
    message: str,
    *,
    path: str | None = None,
    receipt: InstallReceiptV1 | None = None,
) -> Finding:
    return _finding(code, Severity.WARNING, message, path=path, receipt=receipt)


def _finding(
    code: str,
    severity: Severity,
    message: str,
    *,
    path: str | None,
    receipt: InstallReceiptV1 | None,
) -> Finding:
    return Finding(
        code=code,
        severity=severity,
        message=message,
        integration_id=receipt.integration_id if receipt is not None else None,
        package_digest=receipt.package_digest if receipt is not None else None,
        path=path,
    )
