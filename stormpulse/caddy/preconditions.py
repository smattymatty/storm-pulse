"""Agent-start preconditions for the Caddy Integration (CORE-005 decision 5).

The drop-in import check used to raise ``ConfigError`` and abort agent boot -
the bug CORE-005 names. It is now a soft-disable: a failure returns a named
reason that publishes as the Integration's ``disabled_error`` status, and the
agent plus every sibling Integration stay up. This adopts the GARAGE-000
self-disable semantics caddy was missing.
"""

from __future__ import annotations

from stormpulse.caddy.config import CaddyConfig
from stormpulse.caddy.sync import verify_drop_in_imported


def run_preconditions(config: CaddyConfig) -> str | None:
    """Return a disabled reason if the drop-in is not importable, else None.

    Wraps ``verify_drop_in_imported`` (which already covers a missing main
    Caddyfile and a missing import directive) and hands its message straight
    through as the soft-disable reason.
    """
    return verify_drop_in_imported(config.main_caddyfile, config.drop_in_path)
