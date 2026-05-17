"""Caddy admin API integration for custom-domain serving (Phase B).

Present only on regional VPS agents that host customer custom-domain
traffic. The handler is wired through the JobManager pattern; see
``stormpulse/garage/`` for the analogous shape.
"""

from .commands import CELLAR_CUSTOM_DOMAIN_CADDY_SYNC, build_caddy_commands
from .sync import make_caddy_sync_handler, verify_drop_in_imported

__all__ = [
    "CELLAR_CUSTOM_DOMAIN_CADDY_SYNC",
    "build_caddy_commands",
    "make_caddy_sync_handler",
    "verify_drop_in_imported",
]
