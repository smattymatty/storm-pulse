"""Release-side integration package signer (CORE-007 authoring, P2-signer).

This module is **not** part of the on-box agent. It lives at the repo root,
outside ``stormpulse/``, so no private-key handling ever ships in the agent wheel
- the agent's trust store is public-key-only by contract, and this is the mirror
image that produces the bytes it verifies.

It single-sources P1's frozen format and reimplements none of it:

- ``digest.scan_and_hash`` for the package digest (signature-excluded),
- ``trust.signed_payload`` for the exact bytes an Ed25519 signature covers,
- ``layout.canonical_json`` for the detached-signature file bytes,
- ``manifest.parse_manifest`` to read the package's declared identity,
- ``digest.copy_tree`` for a normalized output tree,
- the ``DetachedSignatureV1`` model for the signature field names.

A reimplementation would drift and produce signatures the agent rejects (F6);
importing the frozen functions is what keeps the signer honest. Stdlib +
``cryptography`` only, so no new CORE-001 runtime dependency.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from stormpulse.integrations.external import digest as d
from stormpulse.integrations.external import layout, manifest, trust
from stormpulse.integrations.external.model import DetachedSignatureV1


class SigningError(Exception):
    """The package or key is not signable (bad manifest, key mismatch, or a tree
    that is already signed)."""


def generate_private_key() -> Ed25519PrivateKey:
    """A fresh Ed25519 signing key. Release-side only; never on a node."""
    return Ed25519PrivateKey.generate()


def load_private_key(pem: bytes) -> Ed25519PrivateKey:
    """Load an Ed25519 private key from PEM (release environment key material)."""
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except ValueError as exc:
        raise SigningError(f"unreadable PEM private key: {exc}") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise SigningError("key is not an Ed25519 private key")
    return key


def private_pem(private_key: Ed25519PrivateKey) -> bytes:
    """Serialize a private key to unencrypted PKCS8 PEM (release-side storage)."""
    return private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def public_pem(private_key: Ed25519PrivateKey) -> bytes:
    """The public half as PEM - the material an operator approves with
    ``stormpulse integration publisher add``, or that the enroll seed carries."""
    return private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def fingerprint_of(public_key: Ed25519PublicKey) -> str:
    """The publisher fingerprint: ``sha256:`` + SHA-256 of the raw 32-byte key.
    Identical derivation to the agent's trust store, so the two always agree."""
    raw = public_key.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def sign_tree(source: Path, private_key: Ed25519PrivateKey) -> DetachedSignatureV1:
    """Digest ``source`` (signature-excluded), then sign its exact payload. The
    manifest MUST declare the signing key's fingerprint - the operator approves a
    key, so a package that claims a different signer is a mistake caught here."""
    scan = d.scan_and_hash(source)
    if scan.manifest_bytes is None:
        raise SigningError(f"no {d.MANIFEST_NAME} at the package root")
    parsed = manifest.parse_manifest(scan.manifest_bytes)
    fingerprint = fingerprint_of(private_key.public_key())
    if parsed.publisher_fingerprint != fingerprint:
        raise SigningError(
            f"manifest declares publisher {parsed.publisher_fingerprint} but the "
            f"signing key is {fingerprint}"
        )
    payload = trust.signed_payload(
        scan.package_digest, parsed.integration_id, parsed.version
    )
    signature_bytes = private_key.sign(payload)
    return DetachedSignatureV1(
        publisher_fingerprint=fingerprint,
        package_digest=scan.package_digest,
        signature_b64=base64.b64encode(signature_bytes).decode("ascii"),
    )


def signature_bytes(signature: DetachedSignatureV1) -> bytes:
    """The exact detached-signature file bytes, via P1's canonical JSON. The field
    names come from the model, so the file cannot drift from what the agent parses."""
    return layout.canonical_json(dataclasses.asdict(signature))


def write_signed_package(
    source: Path, dest: Path, private_key: Ed25519PrivateKey
) -> Path:
    """Produce a normalized copy of ``source`` at ``dest`` with a detached
    ``stormpulse.integration.sig`` the P1 loader accepts. ``source`` must not
    already carry a signature (sign an unsigned tree)."""
    if (source / d.SIGNATURE_NAME).exists():
        raise SigningError(
            f"{d.SIGNATURE_NAME} already present in source; sign an unsigned tree"
        )
    signature = sign_tree(source, private_key)
    d.copy_tree(source, dest)
    layout.atomic_write(dest / d.SIGNATURE_NAME, signature_bytes(signature))
    return dest
