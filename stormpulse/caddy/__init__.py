"""Caddy admin API integration for custom-domain serving on regional VPS agents."""

from .cert_status import make_caddy_cert_status_handler
from .commands import (
    BUCKETS_CUSTOM_DOMAIN_CADDY_SYNC,
    CADDY_CERT_STATUS,
    build_caddy_specs,
)
from .sync import make_caddy_sync_handler, verify_drop_in_imported

__all__ = [
    "BUCKETS_CUSTOM_DOMAIN_CADDY_SYNC",
    "CADDY_CERT_STATUS",
    "build_caddy_specs",
    "make_caddy_cert_status_handler",
    "make_caddy_sync_handler",
    "verify_drop_in_imported",
]
