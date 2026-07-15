"""P1 external integration loader (CORE-007).

Declarative inspection, publisher trust, package identity, immutable
installation, and diagnostics for operator-sealed private integrations. This
subpackage **never imports or executes external package code** and grants no
capability; it proves that bytes, signer, and installed artifact can be
identified repeatably and recovered safely before a later stage adds execution
authority.

Framework layer (CORE-000): standard library plus ``cryptography`` only.
"""

from __future__ import annotations

from stormpulse.integrations.external.doctor import doctor_packages
from stormpulse.integrations.external.inspection import inspect_package
from stormpulse.integrations.external.install import install_package
from stormpulse.integrations.external.ledger import list_receipts
from stormpulse.integrations.external.trust import (
    add_publisher,
    list_publishers,
    revoke_publisher,
)

__all__ = [
    "add_publisher",
    "doctor_packages",
    "inspect_package",
    "install_package",
    "list_publishers",
    "list_receipts",
    "revoke_publisher",
]
