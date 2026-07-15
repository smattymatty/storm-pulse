"""Publisher trust store and Ed25519 signature verification.

A publisher record is the local authority for "which key may sign a package".
Records are written only here, under the shared lock, and hold public key
material only (never a private key). Approval and revocation are local operator
acts; a revoked publisher is never implicitly un-revoked.
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from stormpulse.integrations.external import layout
from stormpulse.integrations.external.model import (
    DetachedSignatureV1,
    FailureCode,
    PackageError,
    PublisherRecordV1,
)

_FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SIGNED_PAYLOAD_PREFIX = "stormpulse-package-signature-v1"
_MAX_LABEL_LEN = 80
_RAW_KEY_BYTES = 32


def add_publisher(state_dir: Path, key_file: Path, label: str) -> PublisherRecordV1:
    """Approve a publisher key locally. Idempotent for an identical active record."""
    _validate_label(label)
    raw = _load_raw_public_key(key_file)
    fingerprint = _fingerprint(raw)
    public_key_b64 = base64.b64encode(raw).decode("ascii")
    record_path = _record_path(state_dir, fingerprint)

    with layout.state_lock(state_dir):
        existing = _read_if_present(record_path)
        if existing is not None:
            if existing.revoked_at is not None:
                raise PackageError(
                    FailureCode.F7, "publisher is revoked; approve a new key instead of re-adding"
                )
            if existing.public_key_b64 == public_key_b64 and existing.label == label:
                return existing
            raise PackageError(
                FailureCode.F9, "a publisher with this fingerprint is already approved with different details"
            )
        record = PublisherRecordV1(
            fingerprint=fingerprint,
            public_key_b64=public_key_b64,
            label=label,
            added_at=layout.now_rfc3339(),
        )
        layout.atomic_write(record_path, layout.canonical_json(_to_dict(record)))
        return record


def revoke_publisher(state_dir: Path, fingerprint: str) -> PublisherRecordV1:
    """Revoke a publisher for future trust decisions. Idempotent."""
    _validate_fingerprint(fingerprint)
    record_path = _record_path(state_dir, fingerprint)
    with layout.state_lock(state_dir):
        record = _read_if_present(record_path)
        if record is None:
            raise PackageError(FailureCode.F7, "no such publisher")
        if record.revoked_at is not None:
            return record
        revoked = PublisherRecordV1(
            fingerprint=record.fingerprint,
            public_key_b64=record.public_key_b64,
            label=record.label,
            added_at=record.added_at,
            revoked_at=layout.now_rfc3339(),
        )
        layout.atomic_write(record_path, layout.canonical_json(_to_dict(revoked)))
        return revoked


def list_publishers(state_dir: Path) -> list[PublisherRecordV1]:
    """Every readable publisher record; a corrupt one (F9) is skipped, not fatal,
    so a single bad file cannot brick the listing (doctor reports the corruption)."""
    with layout.state_lock(state_dir):  # lock so we never observe a half-commit
        directory = layout.publishers_dir(state_dir)
        records: list[PublisherRecordV1] = []
        for path in sorted(directory.glob("sha256_*.json")):
            try:
                records.append(_read_record(path))
            except PackageError:
                continue
        return records


def lookup(state_dir: Path, fingerprint: str) -> PublisherRecordV1 | None:
    _validate_fingerprint(fingerprint)
    return _read_if_present(_record_path(state_dir, fingerprint))


def is_active(record: PublisherRecordV1) -> bool:
    """A publisher is active only while it has not been revoked."""
    return record.revoked_at is None


def signed_payload(package_digest: str, integration_id: str, version: str) -> bytes:
    """The exact ASCII bytes an Ed25519 signature covers."""
    return f"{_SIGNED_PAYLOAD_PREFIX}\n{package_digest}\n{integration_id}\n{version}\n".encode("ascii")


def verify_signature(
    record: PublisherRecordV1,
    signature: DetachedSignatureV1,
    *,
    package_digest: str,
    integration_id: str,
    version: str,
) -> bool:
    """Return True only if ``record``'s key signed exactly this package's payload.

    This is a **pure cryptographic** check and deliberately ignores revocation, so
    ``inspect`` can report a valid signature from a revoked publisher accurately.
    A caller authorizing an action (an install) MUST also require
    :func:`is_active`; verification alone never means "trusted".
    """
    if record.fingerprint != signature.publisher_fingerprint:
        return False
    if signature.package_digest != package_digest:
        return False
    try:
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(record.public_key_b64))
        signature_bytes = base64.b64decode(signature.signature_b64)
    except ValueError:
        return False
    try:
        public_key.verify(signature_bytes, signed_payload(package_digest, integration_id, version))
    except InvalidSignature:
        return False
    return True


def _validate_label(label: str) -> None:
    if not 1 <= len(label) <= _MAX_LABEL_LEN:
        raise PackageError(FailureCode.F4, f"label must be 1..{_MAX_LABEL_LEN} characters")
    if any(not char.isprintable() for char in label):
        raise PackageError(FailureCode.F4, "label must not contain control characters")


def _validate_fingerprint(fingerprint: str) -> None:
    if not _FINGERPRINT_RE.match(fingerprint):
        raise PackageError(FailureCode.F4, "fingerprint is not a sha256 digest")


def _fingerprint(raw_public_key: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw_public_key).hexdigest()


def _record_path(state_dir: Path, fingerprint: str) -> Path:
    hex_part = fingerprint.split(":", 1)[1]
    return layout.publishers_dir(state_dir) / f"sha256_{hex_part}.json"


def _load_raw_public_key(key_file: Path) -> bytes:
    data = key_file.read_bytes()
    if data.lstrip().startswith(b"-----BEGIN"):
        if b"PRIVATE KEY" in data:
            raise PackageError(FailureCode.F5, "private key material is not accepted")
        try:
            loaded = serialization.load_pem_public_key(data)
        except ValueError:
            raise PackageError(FailureCode.F5, "unreadable PEM public key") from None
        if not isinstance(loaded, Ed25519PublicKey):
            raise PackageError(FailureCode.F5, "key is not an Ed25519 public key")
        return loaded.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if len(data) == _RAW_KEY_BYTES:
        try:
            key = Ed25519PublicKey.from_public_bytes(data)
        except ValueError:
            raise PackageError(FailureCode.F5, "invalid raw Ed25519 public key") from None
        return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    raise PackageError(FailureCode.F5, "key file is not a raw or PEM Ed25519 public key")


def _read_if_present(path: Path) -> PublisherRecordV1 | None:
    if not path.exists():
        return None
    return _read_record(path)


def _read_record(path: Path) -> PublisherRecordV1:
    try:
        data = layout.read_json(path)
    except (ValueError, OSError):
        raise PackageError(FailureCode.F9, f"publisher record {path.name} is unreadable") from None
    return _from_dict(data, path.name)


def _from_dict(data: Any, name: str) -> PublisherRecordV1:
    if not isinstance(data, dict):
        raise PackageError(FailureCode.F9, f"publisher record {name} is not an object")
    if data.get("schema_version", 1) != 1 or data.get("algorithm", "ed25519") != "ed25519":
        raise PackageError(FailureCode.F9, f"publisher record {name} has an unsupported schema")
    raw_revoked = data.get("revoked_at")
    if raw_revoked is None:
        revoked_at: str | None = None
    elif isinstance(raw_revoked, str):
        revoked_at = raw_revoked
    else:
        raise PackageError(FailureCode.F9, f"publisher record {name} has a malformed revoked_at")
    return PublisherRecordV1(
        fingerprint=_req_str(data, "fingerprint", name),
        public_key_b64=_req_str(data, "public_key_b64", name),
        label=_req_str(data, "label", name),
        added_at=_req_str(data, "added_at", name),
        revoked_at=revoked_at,
    )


def _req_str(data: dict[str, Any], key: str, name: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise PackageError(FailureCode.F9, f"publisher record {name} field '{key}' is invalid")
    return value


def _to_dict(record: PublisherRecordV1) -> dict[str, object]:
    result: dict[str, object] = {
        "schema_version": record.schema_version,
        "algorithm": record.algorithm,
        "fingerprint": record.fingerprint,
        "public_key_b64": record.public_key_b64,
        "label": record.label,
        "added_at": record.added_at,
    }
    if record.revoked_at is not None:
        result["revoked_at"] = record.revoked_at
    return result
