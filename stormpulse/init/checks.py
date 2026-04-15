"""Prerequisite checks and credential parsing for ``stormpulse init``."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

from cryptography import x509
from cryptography.x509.oid import NameOID


class InitError(Exception):
    """Raised when init fails."""


def check_root() -> None:
    """Verify running as root. Raises InitError if not."""
    if os.geteuid() != 0:
        raise InitError(
            "stormpulse init must be run as root (sudo stormpulse init)"
        )


_REQUIRED_CRED_FILES = ("agent.pem", "agent-key.pem", "ca.pem", "hmac.key")


def check_credentials(creds_dir: Path) -> None:
    """Verify all credential files from enrollment exist."""
    if not creds_dir.is_dir():
        raise InitError(
            f"Credentials directory not found: {creds_dir}. "
            f"Run 'stormpulse enroll' first."
        )
    missing = [f for f in _REQUIRED_CRED_FILES if not (creds_dir / f).is_file()]
    if missing:
        raise InitError(
            f"Missing credential files in {creds_dir}: {', '.join(missing)}. "
            f"Run 'stormpulse enroll' first."
        )


def extract_agent_id(creds_dir: Path) -> str:
    """Extract agent ID (CN) from the enrolled certificate."""
    cert_path = creds_dir / "agent.pem"
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if not attrs:
            raise InitError(f"Certificate {cert_path} has no CN attribute")
        return str(attrs[0].value)
    except InitError:
        raise
    except Exception as exc:
        raise InitError(f"Cannot read agent ID from {cert_path}: {exc}") from exc


def load_enroll_metadata(creds_dir: Path) -> dict[str, str]:
    """Load enroll.json written by ``stormpulse enroll``.

    Returns an empty dict if the file is missing or unreadable.
    """
    path = creds_dir / "enroll.json"
    try:
        return dict(json.loads(path.read_text("utf-8")))
    except Exception:  # noqa: BLE001
        return {}


def derive_dashboard_url(endpoint: str) -> str:
    """Derive a default WebSocket dashboard URL from the enrollment endpoint.

    ``https://example.com/api/enroll/`` → ``wss://example.com/ws/pulse/``
    ``http://localhost:8000/api/enroll/`` → ``ws://localhost:8000/ws/pulse/``
    """
    parsed = urlparse(endpoint)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    host = parsed.hostname or ""
    port = parsed.port
    if port and port not in (80, 443):
        netloc = f"{host}:{port}"
    else:
        netloc = host
    return f"{scheme}://{netloc}/ws/pulse/"
