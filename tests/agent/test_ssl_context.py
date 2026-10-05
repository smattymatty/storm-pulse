"""Tests for ``stormpulse.agent.create_ssl_context``."""

from __future__ import annotations

import logging
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from stormpulse.agent import create_ssl_context
from stormpulse.agent.ssl_context import load_tls_context
from stormpulse.config import TlsConfig


@patch("stormpulse.agent.ssl_context.ssl.create_default_context")
def test_create_ssl_context(mock_ctx_factory: MagicMock) -> None:
    mock_ctx = MagicMock(spec=ssl.SSLContext)
    mock_ctx_factory.return_value = mock_ctx
    tls = TlsConfig(
        ca_cert=Path("/ca.pem"),
        client_cert=Path("/agent.pem"),
        client_key=Path("/key.pem"),
    )

    result = create_ssl_context(tls)

    mock_ctx_factory.assert_called_once_with()
    mock_ctx.load_verify_locations.assert_called_once_with(cafile="/ca.pem")
    mock_ctx.load_cert_chain.assert_called_once_with(
        certfile="/agent.pem",
        keyfile="/key.pem",
    )
    assert result is mock_ctx


def _write_pair(cert_path: Path, key_path: Path) -> bytes:
    """Write a self-signed cert and its key; return the cert PEM."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "agent-1")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM)
    cert_path.write_bytes(pem)
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return pem


class TestPrevFallback:
    """Boot falls back to ``.prev`` when the live pair will not load (CORE-010 d3)."""

    @staticmethod
    def _tls(tmp_path: Path) -> TlsConfig:
        tls = TlsConfig(
            ca_cert=tmp_path / "ca.pem",
            client_cert=tmp_path / "agent.pem",
            client_key=tmp_path / "agent-key.pem",
        )
        ca = _write_pair(tls.ca_cert, tmp_path / "ca-key.pem")
        tls.ca_cert.write_bytes(ca)
        return tls

    @staticmethod
    def _prev(path: Path) -> Path:
        return path.with_name(path.name + ".prev")

    @staticmethod
    def _loaded(tls: TlsConfig) -> tuple[ssl.SSLContext, list[str]]:
        real = ssl.SSLContext.load_cert_chain
        loaded: list[str] = []

        def spy(self: ssl.SSLContext, certfile: str, keyfile: str) -> None:
            real(self, certfile, keyfile)
            loaded.append(certfile)

        with patch.object(ssl.SSLContext, "load_cert_chain", spy):
            return create_ssl_context(tls), loaded

    def test_good_live_pair_never_reads_prev(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        tls = self._tls(tmp_path)
        _write_pair(tls.client_cert, tls.client_key)
        with caplog.at_level(logging.ERROR):
            _, loaded = self._loaded(tls)
        assert loaded == [str(tls.client_cert)]
        assert caplog.records == []

    def test_corrupt_live_cert_boots_on_prev_and_logs_both(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        tls = self._tls(tmp_path)
        _write_pair(self._prev(tls.client_cert), self._prev(tls.client_key))
        _write_pair(tls.client_cert, tls.client_key)
        tls.client_cert.write_bytes(b"-----BEGIN CERTIFICATE-----\ngarbage\n")
        with caplog.at_level(logging.ERROR):
            _, loaded = self._loaded(tls)
        assert loaded == [str(self._prev(tls.client_cert))]
        [record] = caplog.records
        assert record.levelno == logging.ERROR
        named = set(record.args or ())
        assert {tls.client_cert, tls.client_key} <= named
        assert {self._prev(tls.client_cert), self._prev(tls.client_key)} <= named

    def test_crash_mid_swap_mismatch_boots_on_prev(self, tmp_path: Path) -> None:
        """New cert live, old key live: the pair a crash between renames leaves."""
        tls = self._tls(tmp_path)
        _write_pair(self._prev(tls.client_cert), self._prev(tls.client_key))
        _write_pair(tls.client_cert, tls.client_key)
        _write_pair(tls.client_cert, tmp_path / "pending-key.pem")
        _, loaded = self._loaded(tls)
        assert loaded == [str(self._prev(tls.client_cert))]

    def test_both_corrupt_raises_the_live_error(self, tmp_path: Path) -> None:
        tls = self._tls(tmp_path)
        tls.client_cert.write_bytes(b"not a cert")
        tls.client_key.write_bytes(b"not a key")
        with pytest.raises(ssl.SSLError) as today:  # the pre-fallback load
            ssl.create_default_context().load_cert_chain(
                certfile=str(tls.client_cert), keyfile=str(tls.client_key)
            )
        self._prev(tls.client_cert).write_bytes(b"also not a cert")
        self._prev(tls.client_key).write_bytes(b"also not a key")
        with pytest.raises(ssl.SSLError) as fallen:
            create_ssl_context(tls)
        assert type(fallen.value) is type(today.value)
        assert fallen.value.__cause__ is None
        assert str(fallen.value) == str(today.value)

    def test_missing_live_cert_boots_on_prev(self, tmp_path: Path) -> None:
        tls = self._tls(tmp_path)
        _write_pair(self._prev(tls.client_cert), self._prev(tls.client_key))
        _, loaded = self._loaded(tls)
        assert loaded == [str(self._prev(tls.client_cert))]

    def test_corrupt_live_without_prev_raises_ssl_error(self, tmp_path: Path) -> None:
        """Never renewed: no ``.prev``, so boot fails as before, not FileNotFound."""
        tls = self._tls(tmp_path)
        _write_pair(tls.client_cert, tls.client_key)
        tls.client_key.write_bytes(b"not a key")
        with pytest.raises(ssl.SSLError):
            create_ssl_context(tls)


class TestLoadedTls:
    """The context says which pair it holds, read from that pair (CORE-010 d4)."""

    _tls = staticmethod(TestPrevFallback._tls)

    def test_live_pair_reports_live_serial_and_expiry(self, tmp_path: Path) -> None:
        tls = self._tls(tmp_path)
        pem = _write_pair(tls.client_cert, tls.client_key)
        cert = x509.load_pem_x509_certificate(pem)
        loaded = load_tls_context(tls)
        assert (loaded.cert, loaded.on_prev) == (tls.client_cert, False)
        assert loaded.serial == cert.serial_number
        assert loaded.not_after == cert.not_valid_after_utc

    def test_fallback_reports_prev_not_the_live_file(self, tmp_path: Path) -> None:
        tls = self._tls(tmp_path)
        prev_cert = tls.client_cert.with_name("agent.pem.prev")
        pem = _write_pair(prev_cert, tls.client_key.with_name("agent-key.pem.prev"))
        _write_pair(tls.client_cert, tmp_path / "stray-key.pem")
        tls.client_key.write_bytes(b"not a key")
        loaded = load_tls_context(tls)
        assert (loaded.cert, loaded.on_prev) == (prev_cert, True)
        assert loaded.serial == x509.load_pem_x509_certificate(pem).serial_number

    def test_boot_finishes_an_interrupted_swap(self, tmp_path: Path) -> None:
        tls = self._tls(tmp_path)
        _write_pair(tls.client_cert, tls.client_key)
        pending = tls.client_key.with_name("agent-key.pem.new")
        _write_pair(tls.client_cert, pending)  # new cert live, its key pending
        pending_pem = pending.read_bytes()
        loaded = load_tls_context(tls)
        assert loaded.on_prev is False
        assert tls.client_key.read_bytes() == pending_pem
        assert not pending.exists()
