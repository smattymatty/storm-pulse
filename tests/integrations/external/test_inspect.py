"""Tests for declarative inspection (never imports package code)."""

from __future__ import annotations

from pathlib import Path

import pytest

from stormpulse.integrations.external import digest, inspection, trust
from stormpulse.integrations.external.model import (
    FailureCode,
    PackageError,
    SignatureStatus,
    TrustStatus,
)
from tests.integrations.external._helpers import approve, keypair, make_package, state_dir


def test_inspect_trusted_and_valid(tmp_path: Path) -> None:
    private, fingerprint = keypair()
    state = state_dir(tmp_path)
    approve(state, tmp_path, private)
    src = tmp_path / "src"
    package_digest = make_package(src, private, fingerprint)

    report = inspection.inspect_package(src, state)
    assert report.package_digest == package_digest
    assert report.trust_status is TrustStatus.TRUSTED
    assert report.signature_status is SignatureStatus.VALID
    assert report.manifest is not None and report.manifest.integration_id == "obs"
    assert report.executable_code_loaded is False


def test_inspect_unknown_publisher_is_unverifiable(tmp_path: Path) -> None:
    private, fingerprint = keypair()
    state = state_dir(tmp_path)  # publisher never approved
    src = tmp_path / "src"
    make_package(src, private, fingerprint)

    report = inspection.inspect_package(src, state)
    assert report.trust_status is TrustStatus.UNKNOWN
    assert report.signature_status is SignatureStatus.UNVERIFIABLE


def test_inspect_revoked_publisher_reports_valid_signature(tmp_path: Path) -> None:
    private, fingerprint = keypair()
    state = state_dir(tmp_path)
    approve(state, tmp_path, private)
    trust.revoke_publisher(state, fingerprint)
    src = tmp_path / "src"
    make_package(src, private, fingerprint)

    report = inspection.inspect_package(src, state)
    assert report.trust_status is TrustStatus.REVOKED
    assert report.signature_status is SignatureStatus.VALID  # crypto is valid; trust is not
    assert any(finding.code == "publisher_revoked" for finding in report.findings)


def test_inspect_tampered_package_signature_invalid(tmp_path: Path) -> None:
    private, fingerprint = keypair()
    state = state_dir(tmp_path)
    approve(state, tmp_path, private)
    src = tmp_path / "src"
    make_package(src, private, fingerprint)
    (src / "code.py").write_bytes(b"tampered\n")

    report = inspection.inspect_package(src, state)
    assert report.signature_status is SignatureStatus.INVALID


def test_inspect_missing_manifest_is_f4(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / digest.SIGNATURE_NAME).write_bytes(b'{"x": 1}')  # signature present, manifest absent
    with pytest.raises(PackageError) as excinfo:
        inspection.inspect_package(src, state_dir(tmp_path))
    assert excinfo.value.code is FailureCode.F4


def test_inspect_never_executes_package_code(tmp_path: Path) -> None:
    private, fingerprint = keypair()
    state = state_dir(tmp_path)
    approve(state, tmp_path, private)
    src = tmp_path / "src"
    # code that would raise if it were ever imported; inspect must not import it
    make_package(src, private, fingerprint, body=b"raise RuntimeError('must never run')\n")

    report = inspection.inspect_package(src, state)
    assert report.executable_code_loaded is False
    assert report.signature_status is SignatureStatus.VALID
