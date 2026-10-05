"""Build the mutual-TLS context the agent uses on every dashboard connection."""

from __future__ import annotations

import logging
import ssl
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from stormpulse.config import TlsConfig
from stormpulse.enroll import (
    complete_pending_swap,
    prev_path,
    read_cert_not_after,
    read_cert_serial,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LoadedTls:
    """A built context and the cert it presents, read from that cert, not the live file."""

    ctx: ssl.SSLContext
    cert: Path
    serial: int | None
    not_after: datetime | None
    on_prev: bool


def load_tls_context(tls: TlsConfig) -> LoadedTls:
    """Build a mutual TLS context from ``[tls]`` and say which pair is in it.

    An interrupted swap is finished first; a live pair that still will not
    load falls back to ``.prev`` (CORE-010 decision 3). If both fail, the
    live pair's error is raised.
    """
    complete_pending_swap(tls)
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=str(tls.ca_cert))
    cert, key = tls.client_cert, tls.client_key
    loaded = cert
    try:
        ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
    except OSError as live_exc:
        loaded, prev_key = prev_path(cert), prev_path(key)
        try:
            ctx.load_cert_chain(certfile=str(loaded), keyfile=str(prev_key))
        except OSError:
            raise live_exc from None
        logger.error(
            "Live pair %s / %s failed to load (%s); booted on %s / %s",
            cert,
            key,
            live_exc,
            loaded,
            prev_key,
        )
    return LoadedTls(
        ctx=ctx,
        cert=loaded,
        serial=read_cert_serial(loaded),
        not_after=read_cert_not_after(loaded),
        on_prev=loaded != cert,
    )


def create_ssl_context(tls: TlsConfig) -> ssl.SSLContext:
    """The context alone, for callers that do not track which pair loaded."""
    return load_tls_context(tls).ctx
