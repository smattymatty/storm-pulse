"""Handler for ``caddy_cert_status``.

Read-only: answer "does Caddy serve a live, publicly-trusted TLS certificate
for this domain right now?" by doing a localhost TLS handshake against Caddy's
HTTPS listener with SNI set to the domain, then letting the system trust store
judge the presented certificate.

This is the authoritative backstop for the custom-domain CERT_PENDING ->
ACTIVE transition. The primary path is certmagic
cert-lifecycle log events; a dropped log line would otherwise strand the
dashboard at "certificate pending" forever even though the site is serving.
A handshake that verifies under the default context proves the cert is real
(publicly issued, not Caddy's internal fallback CA), in date, and covers the
domain. One outbound connection over loopback, no mutation.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import ssl
import time
from typing import Any

from stormpulse.caddy.config import CaddyConfig
from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback

logger = logging.getLogger(__name__)

_HTTPS_PORT = 443
_TIMEOUT_SECONDS = 5.0


def make_caddy_cert_status_handler(
    _config: CaddyConfig,
    params: dict[str, str],
) -> JobHandler | None:
    """Build a JobHandler from runtime params. Required: ``domain``."""
    domain = (params.get("domain") or "").strip().lower()
    if not domain:
        logger.error("caddy_cert_status missing required param: domain")
        return None

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return await run_caddy_cert_status(progress=progress, domain=domain)

    return handler


async def run_caddy_cert_status(
    progress: ProgressCallback,
    domain: str,
) -> JobOutcome:
    """Probe Caddy's localhost HTTPS listener for a live cert for ``domain``."""
    started_at = time.monotonic()
    await progress("starting", 0, 1, f"Checking certificate for {domain}")

    cert, err = await asyncio.to_thread(_probe_cert, domain)
    if cert is None:
        # Verification failed: no live publicly-trusted cert yet (still
        # provisioning, name mismatch, expired, or Caddy's internal CA). A
        # definite "not live", not a command error - the query succeeded.
        return _result(
            domain, cert_live=False, not_after="", issuer="",
            error=err, started_at=started_at,
        )

    return _result(
        domain, cert_live=True,
        not_after=cert.get("notAfter", ""),
        issuer=_issuer_name(cert),
        error="", started_at=started_at,
    )


def _probe_cert(domain: str) -> tuple[dict[str, Any] | None, str]:
    """Localhost TLS handshake with SNI=domain under the system trust store.

    Returns ``(peer_cert_dict, "")`` when the handshake verifies (a real,
    in-date, name-matching, publicly-trusted cert is served), else
    ``(None, error)``. Never raises. The default context enforces chain
    trust, hostname match, and validity dates, so a clean handshake IS the
    authoritative "cert is live" signal; Caddy's internal fallback CA fails
    chain verification and reads as not-live, which is what we want.
    """
    context = ssl.create_default_context()
    try:
        with socket.create_connection(
            ("127.0.0.1", _HTTPS_PORT), timeout=_TIMEOUT_SECONDS,
        ) as sock:
            with context.wrap_socket(sock, server_hostname=domain) as ssock:
                return ssock.getpeercert(), ""
    except ssl.SSLCertVerificationError as exc:
        return None, f"cert not verifiable: {exc}"
    except (OSError, ssl.SSLError) as exc:
        return None, f"handshake failed: {exc}"


def _issuer_name(cert: dict[str, Any]) -> str:
    """Issuer organizationName (or commonName) from a getpeercert() dict.

    ``issuer`` is a tuple of relative distinguished names, each a tuple of
    ``(key, value)`` pairs. Returns '' if neither field is present.
    """
    for rdn in cert.get("issuer", ()):
        for key, value in rdn:
            if key in ("organizationName", "commonName"):
                return str(value)
    return ""


def _result(
    domain: str, *, cert_live: bool, not_after: str, issuer: str,
    error: str, started_at: float,
) -> JobOutcome:
    return JobOutcome(
        success=True,
        exit_code=0,
        stdout=f"cert for {domain}: {'live' if cert_live else 'not live'}",
        extras={
            "domain": domain,
            "cert_live": cert_live,
            "not_after": not_after,
            "issuer": issuer,
            "probe_error": error,
            "duration_seconds": round(time.monotonic() - started_at, 3),
        },
    )
