"""Integration contract registry - the CORE-005 registration seam.

Framework layer (CORE-000): imports Foundation only. Integrations register
their contract here at import time; the Entry layer iterates the registered
set without importing any Integration by name.
"""

from stormpulse.integrations.registry import (
    Integration,
    register_integration,
    registered_integrations,
)

__all__ = [
    "Integration",
    "register_integration",
    "registered_integrations",
]
