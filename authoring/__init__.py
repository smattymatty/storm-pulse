"""Release-side authoring tools for private Storm Pulse integrations (CORE-007).

Repo-root package, deliberately outside ``stormpulse/``: it is not shipped in the
agent wheel and holds the only private-key handling in the tree. Publishing a
package is ``python -m authoring sign``; see ``authoring/signer.py``.
"""

from __future__ import annotations

from authoring.signer import (
    SigningError,
    fingerprint_of,
    generate_private_key,
    load_private_key,
    private_pem,
    public_pem,
    sign_tree,
    signature_bytes,
    write_signed_package,
)

__all__ = [
    "SigningError",
    "fingerprint_of",
    "generate_private_key",
    "load_private_key",
    "private_pem",
    "public_pem",
    "sign_tree",
    "signature_bytes",
    "write_signed_package",
]
