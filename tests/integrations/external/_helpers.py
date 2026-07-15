"""Shared fixtures for external-loader tests: keypairs, state dirs, signed packages."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from stormpulse.integrations.external import digest, layout, trust


def keypair() -> tuple[Ed25519PrivateKey, str]:
    private = Ed25519PrivateKey.generate()
    raw = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return private, "sha256:" + hashlib.sha256(raw).hexdigest()


def state_dir(tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    return state


def approve(state: Path, tmp_path: Path, private: Ed25519PrivateKey) -> None:
    raw = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    key_file = tmp_path / "key.raw"
    key_file.write_bytes(raw)
    trust.add_publisher(state, key_file, "test key")


def make_package(
    pkg: Path,
    private: Ed25519PrivateKey,
    fingerprint: str,
    *,
    integration_id: str = "obs",
    version: str = "1.0.0",
    body: bytes = b"code\n",
    extra: dict[str, bytes] | None = None,
) -> str:
    pkg.mkdir(parents=True, exist_ok=True)
    manifest_bytes = (
        f'schema_version = 1\n\n[integration]\nid = "{integration_id}"\n'
        f'version = "{version}"\nentry_module = "{integration_id}.integration"\n\n'
        f'[publisher]\nfingerprint = "{fingerprint}"\n\n'
        f'[requests]\ncapabilities = ["integration_load"]\n'
    ).encode()
    (pkg / digest.MANIFEST_NAME).write_bytes(manifest_bytes)
    (pkg / "code.py").write_bytes(body)
    for rel, content in (extra or {}).items():
        path = pkg / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    package_digest = digest.scan_and_hash(pkg).package_digest
    payload = trust.signed_payload(package_digest, integration_id, version)
    signature = {
        "schema_version": 1,
        "algorithm": "ed25519",
        "publisher_fingerprint": fingerprint,
        "package_digest": package_digest,
        "signature_b64": base64.b64encode(private.sign(payload)).decode("ascii"),
    }
    (pkg / digest.SIGNATURE_NAME).write_bytes(json.dumps(signature).encode("utf-8"))
    return package_digest


def installed_dir(state: Path, package_digest: str) -> Path:
    return layout.packages_dir(state) / package_digest.split(":")[1]
