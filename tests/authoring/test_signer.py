"""P2-signer: the release-side signer produces packages the P1 loader accepts,
stays pinned to the frozen golden format, and keeps all private-key handling out
of the agent."""

from __future__ import annotations

import pathlib
from pathlib import Path

import pytest

from stormpulse.integrations.external import digest as d
from stormpulse.integrations.external import inspection, trust
from stormpulse.integrations.external.model import SignatureStatus, TrustStatus
from tests.integrations.external._helpers import approve, state_dir

from authoring import signer

# Mirrors tests/integrations/external/test_golden_vectors.py. If either drifts,
# the signer would emit signatures the agent rejects; this fails loudly here.
_DIGEST_GOLDEN = "sha256:8817db0e8b2a153cfb920e6136f49323440f68b3ebc67ed41901fb517ee0dc93"


def _make_source(
    root: Path, fingerprint: str, *, integration_id: str = "obs_gate", version: str = "1.0.0"
) -> Path:
    src = root / "src"
    (src / integration_id).mkdir(parents=True)
    (src / d.MANIFEST_NAME).write_text(
        f'schema_version = 1\n[integration]\nid = "{integration_id}"\n'
        f'version = "{version}"\nentry_module = "{integration_id}.integration"\n'
        f'[publisher]\nfingerprint = "{fingerprint}"\n',
        encoding="utf-8",
    )
    (src / integration_id / "integration.py").write_text("INTEGRATION = object()\n", encoding="utf-8")
    return src


def test_signed_package_is_accepted_by_the_p1_loader(tmp_path: Path) -> None:
    private_key = signer.generate_private_key()
    fingerprint = signer.fingerprint_of(private_key.public_key())
    src = _make_source(tmp_path, fingerprint)

    dest = signer.write_signed_package(src, tmp_path / "dest", private_key)

    state = state_dir(tmp_path)
    approve(state, tmp_path, private_key)  # operator approves the signer's public key
    report = inspection.inspect_package(dest, state)
    assert report.signature_status is SignatureStatus.VALID
    assert report.trust_status is TrustStatus.TRUSTED
    assert report.package_digest is not None


def test_signer_primitives_match_frozen_golden_vectors(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    (pkg / "sub").mkdir(parents=True)
    (pkg / d.MANIFEST_NAME).write_bytes(b'schema_version = 1\n[integration]\nid = "obs"\n')
    (pkg / "sub" / "handler.py").write_bytes(b"print('hi')\n")
    (pkg / d.SIGNATURE_NAME).write_bytes(b"any-signature-bytes")  # excluded from the digest
    assert d.scan_and_hash(pkg).package_digest == _DIGEST_GOLDEN
    assert trust.signed_payload("sha256:" + "ab" * 32, "obs", "1.2.3") == (
        b"stormpulse-package-signature-v1\n"
        b"sha256:abababababababababababababababababababababababababababababababab\n"
        b"obs\n1.2.3\n"
    )


def test_manifest_must_declare_the_signing_key(tmp_path: Path) -> None:
    signing_key = signer.generate_private_key()
    other_key = signer.generate_private_key()
    # manifest declares `other_key`, but we sign with `signing_key`
    src = _make_source(tmp_path, signer.fingerprint_of(other_key.public_key()))
    with pytest.raises(signer.SigningError):
        signer.sign_tree(src, signing_key)


def test_already_signed_source_is_rejected(tmp_path: Path) -> None:
    private_key = signer.generate_private_key()
    fingerprint = signer.fingerprint_of(private_key.public_key())
    src = _make_source(tmp_path, fingerprint)
    (src / d.SIGNATURE_NAME).write_bytes(b"stale")
    with pytest.raises(signer.SigningError):
        signer.write_signed_package(src, tmp_path / "dest", private_key)


def test_tampering_after_signing_fails_verification(tmp_path: Path) -> None:
    private_key = signer.generate_private_key()
    fingerprint = signer.fingerprint_of(private_key.public_key())
    src = _make_source(tmp_path, fingerprint)
    dest = signer.write_signed_package(src, tmp_path / "dest", private_key)

    # Mutate an installed byte after signing: the digest moves, the signature no
    # longer covers it.
    (dest / "obs_gate" / "integration.py").write_text("INTEGRATION = 'tampered'\n", encoding="utf-8")

    state = state_dir(tmp_path)
    approve(state, tmp_path, private_key)
    report = inspection.inspect_package(dest, state)
    assert report.signature_status is SignatureStatus.INVALID


def test_load_private_key_rejects_non_ed25519(tmp_path: Path) -> None:
    with pytest.raises(signer.SigningError):
        signer.load_private_key(b"-----BEGIN PRIVATE KEY-----\nnope\n-----END PRIVATE KEY-----\n")


def test_no_private_key_handling_in_the_agent() -> None:
    # The architectural guarantee: the agent package holds no private-key handling
    # and never imports the release-side authoring tools.
    import stormpulse

    root = pathlib.Path(stormpulse.__file__).resolve().parent
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "load_pem_private_key" not in text, f"{path} handles a private key"
        assert "import authoring" not in text, f"{path} imports the release-side signer"
