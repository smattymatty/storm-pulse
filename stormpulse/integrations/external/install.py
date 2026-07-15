"""Immutable content-addressed installation.

Copies validated bytes into a private temporary tree, verifies the DESTINATION
hash and the detached signature against an active, approved publisher, then
atomically renames to the digest path and writes a receipt. Authority derives
from the destination hash, so a source that races during the copy cannot get an
unsigned tree blessed. Nothing here imports or executes the package.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

from stormpulse.config import Config
from stormpulse.integrations.external import digest, layout, ledger, manifest, trust
from stormpulse.integrations.external.model import (
    DetachedSignatureV1,
    FailureCode,
    InstallReceiptV1,
    ManifestV1,
    PackageError,
)


def install_package(source: Path, config: Config) -> InstallReceiptV1:
    return commit_install(source, state_dir=config.storage.db_path.parent, agent_id=config.agent.id)


def commit_install(source: Path, *, state_dir: Path, agent_id: str) -> InstallReceiptV1:
    with layout.state_lock(state_dir):
        staging = Path(tempfile.mkdtemp(dir=layout.tmp_dir(state_dir), prefix="install-"))
        package_tmp = staging / "pkg"
        try:
            digest.copy_tree(source, package_tmp)
            scan = digest.scan_and_hash(package_tmp)  # the destination is the sole authority
            if scan.manifest_bytes is None:
                raise PackageError(FailureCode.F4, "package has no manifest")
            if scan.signature_bytes is None:
                raise PackageError(FailureCode.F4, "package has no signature")
            parsed = manifest.parse_manifest(scan.manifest_bytes)
            signature = manifest.parse_signature(scan.signature_bytes)
            manifest_digest = "sha256:" + hashlib.sha256(scan.manifest_bytes).hexdigest()
            _authorize(state_dir, parsed, signature, scan.package_digest)

            hex_part = scan.package_digest.split(":", 1)[1]
            target = layout.packages_dir(state_dir) / hex_part
            if target.exists():
                _verify_existing_target(target, scan.package_digest)
                _reseal(target)  # self-heal any mode drift on idempotent reinstall
            else:
                digest.seal_contents(package_tmp)  # durable + read-only before commit
                os.replace(package_tmp, target)
                os.chmod(target, digest.INSTALLED_DIR_MODE)  # finalize the root mode
                layout.fsync_dir(layout.packages_dir(state_dir))

            receipt = InstallReceiptV1(
                agent_id=agent_id,
                integration_id=parsed.integration_id,
                version=parsed.version,
                sdk_api=parsed.sdk_api,
                package_digest=scan.package_digest,
                manifest_digest=manifest_digest,
                publisher_fingerprint=parsed.publisher_fingerprint,
                requested_capabilities=parsed.requested_capabilities,
                command_specs_digest=parsed.command_specs_digest,
                service_manifest_digest=parsed.service_manifest_digest,
                installed_relpath=f"packages/sha256/{hex_part}",
                installed_at=layout.now_rfc3339(),
            )
            ledger.write_receipt(state_dir, receipt)
            return receipt
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def _authorize(
    state_dir: Path,
    parsed: ManifestV1,
    signature: DetachedSignatureV1,
    package_digest: str,
) -> None:
    if signature.publisher_fingerprint != parsed.publisher_fingerprint:
        raise PackageError(FailureCode.F6, "signature and manifest name different publishers")
    record = trust.lookup(state_dir, parsed.publisher_fingerprint)
    if record is None:
        raise PackageError(FailureCode.F7, "publisher is not approved")
    if not trust.is_active(record):
        raise PackageError(FailureCode.F7, "publisher is revoked")
    if not trust.verify_signature(
        record,
        signature,
        package_digest=package_digest,
        integration_id=parsed.integration_id,
        version=parsed.version,
    ):
        raise PackageError(FailureCode.F6, "signature verification failed")


def _verify_existing_target(target: Path, package_digest: str) -> None:
    # Content-only by design: an idempotent reinstall verifies the tree still
    # hashes to its digest (F10 on mismatch) and relies on the following re-seal
    # to restore modes; doctor reports mode drift out of band.
    if digest.scan_and_hash(target).package_digest != package_digest:
        raise PackageError(FailureCode.F10, "installed digest path is corrupt")


def _reseal(target: Path) -> None:
    digest.seal_contents(target)
    os.chmod(target, digest.INSTALLED_DIR_MODE)
