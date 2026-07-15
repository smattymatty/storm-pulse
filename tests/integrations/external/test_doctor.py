"""Tests for installed-state diagnostics."""

from __future__ import annotations

import base64
import json
import os
import shutil
from pathlib import Path

from stormpulse.integrations.external import digest, doctor, install, layout, trust
from stormpulse.integrations.external.model import Severity
from tests.integrations.external._helpers import (
    approve,
    installed_dir,
    keypair,
    make_package,
    state_dir,
)


def _install_one(tmp_path: Path) -> tuple[Path, str, str]:
    private, fingerprint = keypair()
    state = state_dir(tmp_path)
    approve(state, tmp_path, private)
    src = tmp_path / "src"
    package_digest = make_package(src, private, fingerprint)
    install.commit_install(src, state_dir=state, agent_id="a")
    return state, package_digest, fingerprint


def test_doctor_healthy_has_no_errors(tmp_path: Path) -> None:
    state, _digest, _fingerprint = _install_one(tmp_path)
    findings = doctor.doctor_packages(state)
    assert [f for f in findings if f.severity is Severity.ERROR] == []


def test_doctor_missing_package_is_error(tmp_path: Path) -> None:
    state, package_digest, _fingerprint = _install_one(tmp_path)
    target = installed_dir(state, package_digest)
    os.chmod(target, 0o755)
    shutil.rmtree(target)
    findings = doctor.doctor_packages(state)
    assert any(f.code == "missing_package" and f.severity is Severity.ERROR for f in findings)


def test_doctor_corrupt_package_is_error(tmp_path: Path) -> None:
    state, package_digest, _fingerprint = _install_one(tmp_path)
    target = installed_dir(state, package_digest)
    os.chmod(target, 0o755)
    (target / "injected.py").write_bytes(b"x")
    findings = doctor.doctor_packages(state)
    assert any(f.code == "package_corrupt" and f.severity is Severity.ERROR for f in findings)


def test_doctor_revoked_publisher_is_warning(tmp_path: Path) -> None:
    state, _digest, fingerprint = _install_one(tmp_path)
    trust.revoke_publisher(state, fingerprint)
    findings = doctor.doctor_packages(state)
    assert any(f.code == "publisher_revoked" and f.severity is Severity.WARNING for f in findings)


def test_doctor_orphan_package_is_warning(tmp_path: Path) -> None:
    state = state_dir(tmp_path)
    layout.ensure_layout(state)
    (layout.packages_dir(state) / ("ab" * 32)).mkdir()
    findings = doctor.doctor_packages(state)
    assert any(f.code == "orphan_package" and f.severity is Severity.WARNING for f in findings)


def test_doctor_orphan_temp_is_warning(tmp_path: Path) -> None:
    state = state_dir(tmp_path)
    layout.ensure_layout(state)
    (layout.tmp_dir(state) / "install-leftover").mkdir()
    findings = doctor.doctor_packages(state)
    assert any(f.code == "orphan_temp" and f.severity is Severity.WARNING for f in findings)


def test_doctor_corrupt_receipt_is_error(tmp_path: Path) -> None:
    state = state_dir(tmp_path)
    layout.ensure_layout(state)
    receipt_dir = layout.receipts_dir(state) / "obs"
    receipt_dir.mkdir(parents=True)
    (receipt_dir / ("0" * 64 + ".json")).write_text("not a receipt")
    findings = doctor.doctor_packages(state)
    assert any(f.code == "receipt_corrupt" and f.severity is Severity.ERROR for f in findings)


def test_doctor_malicious_receipt_digest_does_not_traverse(tmp_path: Path) -> None:
    # A tampered digest with a path escape must be reported, never followed or crash.
    state = state_dir(tmp_path)
    layout.ensure_layout(state)
    receipt_dir = layout.receipts_dir(state) / "obs"
    receipt_dir.mkdir(parents=True)
    payload = {
        "schema_version": 1,
        "agent_id": "a",
        "integration_id": "obs",
        "version": "1.0.0",
        "sdk_api": 1,
        "package_digest": "sha256:../../../../etc",
        "manifest_digest": "sha256:" + "ab" * 32,
        "publisher_fingerprint": "sha256:" + "ab" * 32,
        "requested_capabilities": [],
        "installed_relpath": "packages/sha256/whatever",
        "installed_at": "2026-01-01T00:00:00Z",
    }
    (receipt_dir / ("0" * 64 + ".json")).write_text(json.dumps(payload))
    findings = doctor.doctor_packages(state)
    assert any(f.code == "receipt_corrupt" for f in findings)


def test_doctor_swapped_signature_is_error(tmp_path: Path) -> None:
    state, package_digest, fingerprint = _install_one(tmp_path)
    target = installed_dir(state, package_digest)
    os.chmod(target, 0o755)
    sig_file = target / digest.SIGNATURE_NAME
    os.chmod(sig_file, 0o644)
    sig_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "algorithm": "ed25519",
                "publisher_fingerprint": fingerprint,
                "package_digest": package_digest,
                "signature_b64": base64.b64encode(b"\x00" * 64).decode("ascii"),  # valid form, wrong sig
            }
        )
    )
    findings = doctor.doctor_packages(state)
    assert any(f.code == "signature_invalid" and f.severity is Severity.ERROR for f in findings)


def test_doctor_receipt_manifest_digest_mismatch_is_error(tmp_path: Path) -> None:
    state, package_digest, _fingerprint = _install_one(tmp_path)
    hex_part = package_digest.split(":")[1]
    receipt_path = layout.receipts_dir(state) / "obs" / f"{hex_part}.json"
    data = json.loads(receipt_path.read_text())
    data["manifest_digest"] = "sha256:" + "00" * 32  # valid form, wrong value
    receipt_path.write_text(json.dumps(data))
    findings = doctor.doctor_packages(state)
    assert any(f.code == "receipt_mismatch" and f.severity is Severity.ERROR for f in findings)


def test_doctor_deleted_publisher_is_unknown_warning(tmp_path: Path) -> None:
    state, _digest, fingerprint = _install_one(tmp_path)
    (layout.publishers_dir(state) / f"sha256_{fingerprint.split(':')[1]}.json").unlink()
    findings = doctor.doctor_packages(state)
    assert any(f.code == "publisher_unknown" and f.severity is Severity.WARNING for f in findings)


def test_doctor_mode_drift_is_warning(tmp_path: Path) -> None:
    state, package_digest, _fingerprint = _install_one(tmp_path)
    os.chmod(installed_dir(state, package_digest), 0o755)  # writable root (crash window or hand-edit)
    findings = doctor.doctor_packages(state)
    assert any(f.code == "mode_drift" and f.severity is Severity.WARNING for f in findings)
    assert [f for f in findings if f.severity is Severity.ERROR] == []  # content is still healthy


def test_doctor_stray_file_under_receipts_is_warning(tmp_path: Path) -> None:
    state = state_dir(tmp_path)
    layout.ensure_layout(state)
    (layout.receipts_dir(state) / "loose.txt").write_text("junk")
    findings = doctor.doctor_packages(state)
    assert any(f.code == "stray_file" and f.severity is Severity.WARNING for f in findings)


def test_doctor_stray_file_in_receipt_folder_is_warning(tmp_path: Path) -> None:
    state, _digest, _fingerprint = _install_one(tmp_path)
    (layout.receipts_dir(state) / "obs" / "notes.txt").write_text("x")
    findings = doctor.doctor_packages(state)
    assert any(f.code == "stray_file" for f in findings)
