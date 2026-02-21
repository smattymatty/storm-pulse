"""Storm Pulse status — local inspection of agent state."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import psutil
from cryptography import x509


@dataclass(frozen=True, slots=True)
class StatusInfo:
    """Structured status data, testable without parsing output."""

    version: str
    agent_id: str
    config_path: Path
    dashboard_url: str
    cert_expiry: datetime | None
    cert_days_remaining: int | None
    db_path: Path
    db_entry_count: int | None
    pid: int | None


# ---------------------------------------------------------------------------
# Collection helpers — each returns None on failure
# ---------------------------------------------------------------------------


def _read_cert_expiry(cert_path: Path) -> datetime | None:
    """Return the certificate's not-valid-after date, or None on any failure."""
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        return cert.not_valid_after_utc
    except Exception:  # noqa: BLE001
        return None


def _count_nonces(db_path: Path) -> int | None:
    """Return nonce count from the SQLite DB, or None if unreadable."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        try:
            row = conn.execute("SELECT COUNT(*) FROM seen_nonces").fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


def _find_agent_pid() -> int | None:
    """Return PID of a running 'stormpulse run' process, or None."""
    try:
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = proc.info["cmdline"] or []
                if any("stormpulse" in part for part in cmdline) and "run" in cmdline:
                    return int(proc.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:  # noqa: BLE001
        return None
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def collect_status(config_path: Path) -> StatusInfo:
    """Collect all status fields. Never raises — graceful degradation."""
    from stormpulse import __version__
    from stormpulse.config import ConfigError, load_config

    agent_id = "unknown"
    dashboard_url = "unknown"
    cert_path: Path | None = None
    db_path = Path("unknown")

    try:
        config = load_config(config_path)
        agent_id = config.agent.id
        dashboard_url = config.dashboard.url
        cert_path = config.tls.client_cert
        db_path = config.storage.db_path
    except ConfigError:
        pass

    cert_expiry = _read_cert_expiry(cert_path) if cert_path is not None else None

    days_remaining: int | None = None
    if cert_expiry is not None:
        delta = cert_expiry - datetime.now(timezone.utc)
        days_remaining = delta.days

    return StatusInfo(
        version=__version__,
        agent_id=agent_id,
        config_path=config_path,
        dashboard_url=dashboard_url,
        cert_expiry=cert_expiry,
        cert_days_remaining=days_remaining,
        db_path=db_path,
        db_entry_count=_count_nonces(db_path),
        pid=_find_agent_pid(),
    )


_LABEL_WIDTH = 16


def print_status(info: StatusInfo) -> None:
    """Print aligned status summary to stdout."""

    def row(label: str, value: str) -> None:
        print(f"{label + ':':<{_LABEL_WIDTH}}{value}")

    row("Storm Pulse", f"v{info.version}")
    row("Agent ID", info.agent_id)
    row("Config", str(info.config_path))
    row("Dashboard", info.dashboard_url)

    if info.cert_expiry is None:
        cert_str = "unavailable"
    else:
        date_str = info.cert_expiry.strftime("%Y-%m-%d")
        days = info.cert_days_remaining
        if days is not None and days < 0:
            cert_str = f"{date_str} (EXPIRED {abs(days)} days ago)"
        elif days is not None:
            cert_str = f"{date_str} ({days} days)"
        else:
            cert_str = date_str
    row("Certs expire", cert_str)

    if info.db_entry_count is None:
        db_str = f"{info.db_path} (unavailable)"
    else:
        db_str = f"{info.db_path} ({info.db_entry_count} entries)"
    row("Nonce DB", db_str)

    if info.pid is None:
        pid_str = "not running"
    else:
        pid_str = f"{info.pid} (running)"
    row("PID", pid_str)
