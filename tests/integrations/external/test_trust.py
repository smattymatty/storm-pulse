"""Tests for the publisher trust store and Ed25519 verification."""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from stormpulse.integrations.external import layout, trust
from stormpulse.integrations.external.model import (
    DetachedSignatureV1,
    FailureCode,
    PackageError,
)

_PKG = "sha256:" + "cd" * 32


def _keypair() -> tuple[Ed25519PrivateKey, bytes]:
    private = Ed25519PrivateKey.generate()
    raw_pub = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return private, raw_pub


def _raw_key_file(tmp_path: Path, raw_pub: bytes, name: str = "key.raw") -> Path:
    path = tmp_path / name
    path.write_bytes(raw_pub)
    return path


def _fingerprint(raw_pub: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw_pub).hexdigest()


def _sign(private: Ed25519PrivateKey, digest: str, integration_id: str, version: str) -> str:
    payload = trust.signed_payload(digest, integration_id, version)
    return base64.b64encode(private.sign(payload)).decode("ascii")


def test_t16_add_idempotent_list(tmp_path: Path) -> None:
    _, raw_pub = _keypair()
    key_file = _raw_key_file(tmp_path, raw_pub)
    first = trust.add_publisher(tmp_path, key_file, "my key")
    assert first.fingerprint == _fingerprint(raw_pub)
    assert first.revoked_at is None
    assert trust.add_publisher(tmp_path, key_file, "my key") == first  # idempotent
    listed = trust.list_publishers(tmp_path)
    assert [record.fingerprint for record in listed] == [first.fingerprint]


def test_add_same_fingerprint_different_label_is_f9(tmp_path: Path) -> None:
    _, raw_pub = _keypair()
    key_file = _raw_key_file(tmp_path, raw_pub)
    trust.add_publisher(tmp_path, key_file, "one")
    with pytest.raises(PackageError) as excinfo:
        trust.add_publisher(tmp_path, key_file, "two")
    assert excinfo.value.code is FailureCode.F9


def test_t18_revoke_idempotent_and_no_readd(tmp_path: Path) -> None:
    _, raw_pub = _keypair()
    key_file = _raw_key_file(tmp_path, raw_pub)
    record = trust.add_publisher(tmp_path, key_file, "k")
    revoked = trust.revoke_publisher(tmp_path, record.fingerprint)
    assert revoked.revoked_at is not None
    assert trust.revoke_publisher(tmp_path, record.fingerprint).revoked_at == revoked.revoked_at
    with pytest.raises(PackageError) as excinfo:
        trust.add_publisher(tmp_path, key_file, "k")
    assert excinfo.value.code is FailureCode.F7


def test_revoke_unknown_is_f7(tmp_path: Path) -> None:
    with pytest.raises(PackageError) as excinfo:
        trust.revoke_publisher(tmp_path, "sha256:" + "00" * 32)
    assert excinfo.value.code is FailureCode.F7


def test_lookup_unknown_is_none(tmp_path: Path) -> None:
    assert trust.lookup(tmp_path, "sha256:" + "00" * 32) is None


def test_lookup_reflects_revocation(tmp_path: Path) -> None:
    _, raw_pub = _keypair()
    record = trust.add_publisher(tmp_path, _raw_key_file(tmp_path, raw_pub), "k")
    assert trust.lookup(tmp_path, record.fingerprint) == record  # active
    trust.revoke_publisher(tmp_path, record.fingerprint)
    looked = trust.lookup(tmp_path, record.fingerprint)
    assert looked is not None and looked.revoked_at is not None


def test_pem_and_raw_derive_same_fingerprint(tmp_path: Path) -> None:
    private, raw_pub = _keypair()
    pem = private.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    pem_file = tmp_path / "key.pem"
    pem_file.write_bytes(pem)
    record = trust.add_publisher(tmp_path, pem_file, "pem key")
    assert record.fingerprint == _fingerprint(raw_pub)


def test_private_key_rejected_f5(tmp_path: Path) -> None:
    private, _ = _keypair()
    pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    key_file = tmp_path / "priv.pem"
    key_file.write_bytes(pem)
    with pytest.raises(PackageError) as excinfo:
        trust.add_publisher(tmp_path, key_file, "k")
    assert excinfo.value.code is FailureCode.F5


def test_bad_raw_length_rejected_f5(tmp_path: Path) -> None:
    key_file = _raw_key_file(tmp_path, b"\x01" * 31)
    with pytest.raises(PackageError) as excinfo:
        trust.add_publisher(tmp_path, key_file, "k")
    assert excinfo.value.code is FailureCode.F5


def test_empty_label_rejected(tmp_path: Path) -> None:
    _, raw_pub = _keypair()
    key_file = _raw_key_file(tmp_path, raw_pub)
    with pytest.raises(PackageError) as excinfo:
        trust.add_publisher(tmp_path, key_file, "")
    assert excinfo.value.code is FailureCode.F4


def test_t11_valid_signature(tmp_path: Path) -> None:
    private, raw_pub = _keypair()
    record = trust.add_publisher(tmp_path, _raw_key_file(tmp_path, raw_pub), "k")
    signature = DetachedSignatureV1(
        publisher_fingerprint=record.fingerprint,
        package_digest=_PKG,
        signature_b64=_sign(private, _PKG, "obs", "1.0.0"),
    )
    assert trust.verify_signature(
        record, signature, package_digest=_PKG, integration_id="obs", version="1.0.0"
    )


def test_t12_wrong_version_fails(tmp_path: Path) -> None:
    private, raw_pub = _keypair()
    record = trust.add_publisher(tmp_path, _raw_key_file(tmp_path, raw_pub), "k")
    signature = DetachedSignatureV1(
        publisher_fingerprint=record.fingerprint,
        package_digest=_PKG,
        signature_b64=_sign(private, _PKG, "obs", "1.0.0"),
    )
    assert not trust.verify_signature(
        record, signature, package_digest=_PKG, integration_id="obs", version="2.0.0"
    )


def test_t12_wrong_key_fails(tmp_path: Path) -> None:
    private, raw_pub = _keypair()
    record = trust.add_publisher(tmp_path, _raw_key_file(tmp_path, raw_pub), "k")
    other_private, _ = _keypair()
    signature = DetachedSignatureV1(
        publisher_fingerprint=record.fingerprint,
        package_digest=_PKG,
        signature_b64=_sign(other_private, _PKG, "obs", "1.0.0"),
    )
    assert not trust.verify_signature(
        record, signature, package_digest=_PKG, integration_id="obs", version="1.0.0"
    )


def test_t12_wrong_digest_fails(tmp_path: Path) -> None:
    private, raw_pub = _keypair()
    record = trust.add_publisher(tmp_path, _raw_key_file(tmp_path, raw_pub), "k")
    other_digest = "sha256:" + "ef" * 32
    signature = DetachedSignatureV1(
        publisher_fingerprint=record.fingerprint,
        package_digest=other_digest,
        signature_b64=_sign(private, other_digest, "obs", "1.0.0"),
    )
    # signature is internally consistent but for a different package than we verify against
    assert not trust.verify_signature(
        record, signature, package_digest=_PKG, integration_id="obs", version="1.0.0"
    )


def test_canonical_json_is_sorted_and_newline_terminated() -> None:
    encoded = layout.canonical_json({"b": 1, "a": 2})
    assert encoded == b'{"a":2,"b":1}\n'


def test_is_active_reflects_revocation(tmp_path: Path) -> None:
    _, raw_pub = _keypair()
    record = trust.add_publisher(tmp_path, _raw_key_file(tmp_path, raw_pub), "k")
    assert trust.is_active(record) is True
    revoked = trust.revoke_publisher(tmp_path, record.fingerprint)
    assert trust.is_active(revoked) is False
