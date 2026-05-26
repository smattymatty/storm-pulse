"""Build the mutual-TLS context the agent uses on every dashboard connection."""

from __future__ import annotations

import ssl

from stormpulse.config import TlsConfig


def create_ssl_context(tls: TlsConfig) -> ssl.SSLContext:
    """Build a mutual TLS context from the three cert paths in ``[tls]``."""
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=str(tls.ca_cert))
    ctx.load_cert_chain(certfile=str(tls.client_cert), keyfile=str(tls.client_key))
    return ctx
