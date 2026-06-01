"""Tests for stormpulse.status."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import psutil
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from stormpulse.status import (
    StatusInfo,
    _count_nonces,
    _find_agent_pid,
    _read_cert_expiry,
    collect_status,
    print_status,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_VALID = """\
[agent]
id = "test-01"
pulse_token = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

[dashboard]
url = "wss://example.com/ws/"
reconnect_min_seconds = 1
reconnect_max_seconds = 30
heartbeat_interval_seconds = 30

[tls]
ca_cert = "{ca_cert}"
client_cert = "{client_cert}"
client_key = "{client_key}"

[auth]
hmac_secret = "{hmac_secret}"
command_max_age_seconds = 60

[metrics]
push_interval_seconds = 10
collect_containers = false

[project]
project_dir = "/tmp/project"
compose_file = "/tmp/project/docker-compose.yml"
docker_service_name = "web"

[storage]
db_path = "{db_path}"
"""


def _make_cert(tmp_path: Path, days: int = 90) -> Path:
    """Generate a self-signed cert valid for `days` from now."""
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-agent")])
        )
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-ca")]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=days))
        .sign(key, hashes.SHA256())
    )
    p = tmp_path / "agent.pem"
    p.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return p


def _make_key(tmp_path: Path) -> Path:
    """Generate an EC private key file."""
    key = ec.generate_private_key(ec.SECP256R1())
    p = tmp_path / "agent-key.pem"
    p.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return p


@pytest.fixture
def cert_path(tmp_path: Path) -> Path:
    return _make_cert(tmp_path)


@pytest.fixture
def nonce_db(tmp_path: Path) -> Path:
    db = tmp_path / "stormpulse.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE seen_nonces (nonce TEXT PRIMARY KEY, seen_at REAL NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO seen_nonces VALUES (?, ?)",
        [(f"nonce-{i}", float(i)) for i in range(5)],
    )
    conn.commit()
    conn.close()
    return db


# ---------------------------------------------------------------------------
# _read_cert_expiry
# ---------------------------------------------------------------------------


class TestReadCertExpiry:
    def test_reads_real_cert(self, cert_path: Path) -> None:
        result = _read_cert_expiry(cert_path)
        assert isinstance(result, datetime)
        assert result > datetime.now(UTC)

    def test_returns_none_for_missing_file(self, tmp_path: Path) -> None:
        assert _read_cert_expiry(tmp_path / "nonexistent.pem") is None

    def test_returns_none_for_garbage(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.pem"
        p.write_bytes(b"not a certificate")
        assert _read_cert_expiry(p) is None

    def test_result_is_utc_aware(self, cert_path: Path) -> None:
        result = _read_cert_expiry(cert_path)
        assert result is not None
        assert result.tzinfo is not None


# ---------------------------------------------------------------------------
# _count_nonces
# ---------------------------------------------------------------------------


class TestCountNonces:
    def test_counts_entries(self, nonce_db: Path) -> None:
        assert _count_nonces(nonce_db) == 5

    def test_returns_zero_for_empty_db(self, tmp_path: Path) -> None:
        db = tmp_path / "empty.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE seen_nonces (nonce TEXT PRIMARY KEY, seen_at REAL NOT NULL)"
        )
        conn.commit()
        conn.close()
        assert _count_nonces(db) == 0

    def test_returns_none_for_missing_db(self, tmp_path: Path) -> None:
        assert _count_nonces(tmp_path / "ghost.db") is None

    def test_does_not_create_missing_db(self, tmp_path: Path) -> None:
        missing = tmp_path / "ghost.db"
        _count_nonces(missing)
        assert not missing.exists()


# ---------------------------------------------------------------------------
# _find_agent_pid
# ---------------------------------------------------------------------------


class TestFindAgentPid:
    @patch("stormpulse.status.psutil")
    def test_finds_running_process(self, mock_psutil: MagicMock) -> None:
        proc = MagicMock()
        proc.info = {"pid": 1842, "cmdline": ["python", "-m", "stormpulse", "run"]}
        mock_psutil.process_iter.return_value = [proc]
        mock_psutil.NoSuchProcess = psutil.NoSuchProcess
        mock_psutil.AccessDenied = psutil.AccessDenied
        assert _find_agent_pid() == 1842

    @patch("stormpulse.status.psutil")
    def test_returns_none_when_not_running(self, mock_psutil: MagicMock) -> None:
        proc = MagicMock()
        proc.info = {"pid": 999, "cmdline": ["python", "other_script.py"]}
        mock_psutil.process_iter.return_value = [proc]
        mock_psutil.NoSuchProcess = psutil.NoSuchProcess
        mock_psutil.AccessDenied = psutil.AccessDenied
        assert _find_agent_pid() is None

    @patch("stormpulse.status.psutil")
    def test_ignores_non_run_subcommand(self, mock_psutil: MagicMock) -> None:
        proc = MagicMock()
        proc.info = {"pid": 999, "cmdline": ["stormpulse", "status"]}
        mock_psutil.process_iter.return_value = [proc]
        mock_psutil.NoSuchProcess = psutil.NoSuchProcess
        mock_psutil.AccessDenied = psutil.AccessDenied
        assert _find_agent_pid() is None

    @patch("stormpulse.status.psutil")
    def test_returns_none_on_error(self, mock_psutil: MagicMock) -> None:
        mock_psutil.process_iter.side_effect = RuntimeError("psutil broken")
        assert _find_agent_pid() is None


# ---------------------------------------------------------------------------
# collect_status
# ---------------------------------------------------------------------------


class TestCollectStatus:
    def test_with_valid_config(self, tmp_path: Path, nonce_db: Path) -> None:
        cert = _make_cert(tmp_path)
        key = _make_key(tmp_path)
        ca = tmp_path / "ca.pem"
        ca.write_bytes(cert.read_bytes())  # self-signed, reuse as CA
        hmac = tmp_path / "hmac.key"
        hmac.write_bytes(b"secret-key-bytes")

        config_content = MINIMAL_VALID.format(
            ca_cert=ca,
            client_cert=cert,
            client_key=key,
            hmac_secret=hmac,
            db_path=nonce_db,
        )
        config_path = tmp_path / "stormpulse.toml"
        config_path.write_text(config_content)

        with patch("stormpulse.status._find_agent_pid", return_value=None):
            info = collect_status(config_path)

        assert info.agent_id == "test-01"
        assert info.dashboard_url == "wss://example.com/ws/"
        assert info.cert_expiry is not None
        assert info.cert_days_remaining is not None
        assert info.cert_days_remaining > 0
        assert info.db_entry_count == 5
        assert info.config_path == config_path

    def test_missing_config_graceful(self, tmp_path: Path) -> None:
        with patch("stormpulse.status._find_agent_pid", return_value=None):
            info = collect_status(tmp_path / "nonexistent.toml")

        assert info.agent_id == "unknown"
        assert info.dashboard_url == "unknown"
        assert info.cert_expiry is None
        assert info.db_entry_count is None

    def test_version_field(self, tmp_path: Path) -> None:
        from stormpulse import __version__

        with patch("stormpulse.status._find_agent_pid", return_value=None):
            info = collect_status(tmp_path / "nonexistent.toml")

        assert info.version == __version__

    def test_config_path_always_preserved(self, tmp_path: Path) -> None:
        path = tmp_path / "my-config.toml"
        with patch("stormpulse.status._find_agent_pid", return_value=None):
            info = collect_status(path)
        assert info.config_path == path


# ---------------------------------------------------------------------------
# print_status
# ---------------------------------------------------------------------------


class TestPrintStatus:
    def test_running_agent(self, capsys: pytest.CaptureFixture[str]) -> None:
        info = StatusInfo(
            version="0.1.0",
            agent_id="vps-toronto-01",
            config_path=Path("/etc/stormpulse/stormpulse.toml"),
            dashboard_url="wss://stormdevelopments.ca/ws/pulse/",
            cert_expiry=datetime(2026, 5, 22, tzinfo=UTC),
            cert_days_remaining=90,
            db_path=Path("/opt/stormpulse/data/stormpulse.db"),
            db_entry_count=247,
            pid=1842,
        )
        print_status(info)
        out = capsys.readouterr().out
        assert "v0.1.0" in out
        assert "vps-toronto-01" in out
        assert "wss://stormdevelopments.ca/ws/pulse/" in out
        assert "2026-05-22 (90 days)" in out
        assert "247 entries" in out
        assert "1842 (running)" in out

    def test_not_running(self, capsys: pytest.CaptureFixture[str]) -> None:
        info = StatusInfo(
            version="0.1.0",
            agent_id="test",
            config_path=Path("/tmp/c.toml"),
            dashboard_url="wss://x/",
            cert_expiry=None,
            cert_days_remaining=None,
            db_path=Path("/tmp/db"),
            db_entry_count=0,
            pid=None,
        )
        print_status(info)
        out = capsys.readouterr().out
        assert "not running" in out

    def test_cert_unavailable(self, capsys: pytest.CaptureFixture[str]) -> None:
        info = StatusInfo(
            version="0.1.0",
            agent_id="test",
            config_path=Path("/tmp/c.toml"),
            dashboard_url="wss://x/",
            cert_expiry=None,
            cert_days_remaining=None,
            db_path=Path("/tmp/db"),
            db_entry_count=0,
            pid=None,
        )
        print_status(info)
        out = capsys.readouterr().out
        assert "unavailable" in out

    def test_db_unavailable(self, capsys: pytest.CaptureFixture[str]) -> None:
        info = StatusInfo(
            version="0.1.0",
            agent_id="test",
            config_path=Path("/tmp/c.toml"),
            dashboard_url="wss://x/",
            cert_expiry=None,
            cert_days_remaining=None,
            db_path=Path("/tmp/db"),
            db_entry_count=None,
            pid=None,
        )
        print_status(info)
        out = capsys.readouterr().out
        assert "(unavailable)" in out

    def test_expired_cert(self, capsys: pytest.CaptureFixture[str]) -> None:
        info = StatusInfo(
            version="0.1.0",
            agent_id="test",
            config_path=Path("/tmp/c.toml"),
            dashboard_url="wss://x/",
            cert_expiry=datetime(2025, 1, 15, tzinfo=UTC),
            cert_days_remaining=-37,
            db_path=Path("/tmp/db"),
            db_entry_count=0,
            pid=None,
        )
        print_status(info)
        out = capsys.readouterr().out
        assert "EXPIRED" in out
        assert "37 days ago" in out

    def test_label_alignment(self, capsys: pytest.CaptureFixture[str]) -> None:
        info = StatusInfo(
            version="0.1.0",
            agent_id="test",
            config_path=Path("/tmp/c.toml"),
            dashboard_url="wss://x/",
            cert_expiry=None,
            cert_days_remaining=None,
            db_path=Path("/tmp/db"),
            db_entry_count=0,
            pid=None,
        )
        print_status(info)
        lines = capsys.readouterr().out.strip().splitlines()
        # Every line should have content starting at the same column
        for line in lines:
            colon_pos = line.index(":")
            # Value text starts after the colon + padding, all at column 16
            assert len(line[: colon_pos + 1].ljust(16)) == 16
