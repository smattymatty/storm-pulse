"""Pinned golden vectors for the on-wire and on-disk byte formats.

A golden vector freezes an exact byte string. The reference-recomputation tests
elsewhere prove the implementation matches the intended *formula*; these prove
the formula itself has not silently changed. A change here is a format break that
invalidates every previously written digest, receipt, and signature, so it must
be a deliberate, versioned decision, never an accidental refactor. The constants
below were computed once from the format definitions and are frozen.
"""

from __future__ import annotations

from pathlib import Path

from stormpulse.integrations.external import digest as d
from stormpulse.integrations.external import layout, trust

# A fixed included set and the digest it must always produce. Signature-excluded
# by definition; the tree below adds a signature file to prove it does not move
# the digest.
_GOLDEN_MANIFEST = b'schema_version = 1\n[integration]\nid = "obs"\n'
_GOLDEN_HANDLER = b"print('hi')\n"
_DIGEST_GOLDEN = "sha256:8817db0e8b2a153cfb920e6136f49323440f68b3ebc67ed41901fb517ee0dc93"


def test_package_digest_golden_vector(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    (pkg / "sub").mkdir(parents=True)
    (pkg / d.MANIFEST_NAME).write_bytes(_GOLDEN_MANIFEST)
    (pkg / "sub" / "handler.py").write_bytes(_GOLDEN_HANDLER)
    (pkg / d.SIGNATURE_NAME).write_bytes(b"any-signature-bytes")  # excluded from the digest
    assert d.scan_and_hash(pkg).package_digest == _DIGEST_GOLDEN


def test_canonical_json_golden_vector() -> None:
    # Keys sort; separators are tight; non-ASCII stays UTF-8 (ensure_ascii=False);
    # the object is newline-terminated. These are receipt/publisher bytes on disk.
    obj = {"b": 2, "a": [3, 1], "z": "é"}
    assert layout.canonical_json(obj) == b'{"a":[3,1],"b":2,"z":"\xc3\xa9"}\n'


def test_signed_payload_golden_vector() -> None:
    # The exact ASCII an Ed25519 signature covers. A drift silently invalidates
    # every publisher's existing signatures, so it is frozen here.
    payload = trust.signed_payload("sha256:" + "ab" * 32, "obs", "1.2.3")
    assert payload == (
        b"stormpulse-package-signature-v1\n"
        b"sha256:abababababababababababababababababababababababababababababababab\n"
        b"obs\n1.2.3\n"
    )
