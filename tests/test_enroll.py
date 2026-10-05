"""Tests for stormpulse.enroll."""

from __future__ import annotations

import base64
import email.message
import json
import os
import stat
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from stormpulse.config import TlsConfig
from stormpulse.enroll import (
    RENEW_TIMEOUT_SECONDS,
    EnrollError,
    RenewError,
    build_csr,
    complete_pending_swap,
    days_remaining,
    generate_keypair,
    pending_key_path,
    preflight_creds_dir,
    presented_cert,
    read_cert_not_after,
    read_cert_serial,
    renew_certificate,
    renew_endpoint,
    request_certificate,
    write_credentials,
    write_enroll_metadata,
)
from stormpulse.init.mode import InstallMode

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_response(hmac_key: str | None = None) -> dict[str, str]:
    if hmac_key is None:
        hmac_key = base64.b64encode(b"test-hmac-key-32-bytes-long!!!!!").decode()
    return {
        "client_cert_pem": "-----BEGIN CERTIFICATE-----\nMOCK\n-----END CERTIFICATE-----\n",
        "ca_cert_pem": "-----BEGIN CERTIFICATE-----\nMOCKCA\n-----END CERTIFICATE-----\n",
        "hmac_key": hmac_key,
        "dashboard_url": "wss://pulse.example.com/ws/pulse/",
    }


def _mock_urlopen(response_data: dict[str, str]) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(response_data).encode()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


# ---------------------------------------------------------------------------
# Key generation
# ---------------------------------------------------------------------------


class TestGenerateKeypair:
    def test_returns_ec_p256_key(self) -> None:
        private_key, _ = generate_keypair()
        assert isinstance(private_key, ec.EllipticCurvePrivateKey)
        assert isinstance(private_key.curve, ec.SECP256R1)

    def test_returns_valid_pem(self) -> None:
        _, key_pem = generate_keypair()
        assert key_pem.startswith(b"-----BEGIN PRIVATE KEY-----")
        loaded = serialization.load_pem_private_key(key_pem, password=None)
        assert isinstance(loaded, ec.EllipticCurvePrivateKey)

    def test_unique_each_call(self) -> None:
        _, pem1 = generate_keypair()
        _, pem2 = generate_keypair()
        assert pem1 != pem2


# ---------------------------------------------------------------------------
# CSR construction
# ---------------------------------------------------------------------------


class TestBuildCsr:
    def test_cn_matches_agent_id(self) -> None:
        key, _ = generate_keypair()
        csr_pem = build_csr(key, "vps-toronto-01")
        csr = x509.load_pem_x509_csr(csr_pem)
        cn = csr.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0].value
        assert cn == "vps-toronto-01"

    def test_valid_pem_format(self) -> None:
        key, _ = generate_keypair()
        csr_pem = build_csr(key, "test-agent")
        assert csr_pem.startswith(b"-----BEGIN CERTIFICATE REQUEST-----")

    def test_signature_is_valid(self) -> None:
        key, _ = generate_keypair()
        csr_pem = build_csr(key, "test-agent")
        csr = x509.load_pem_x509_csr(csr_pem)
        assert csr.is_signature_valid

    def test_uses_sha256(self) -> None:
        key, _ = generate_keypair()
        csr_pem = build_csr(key, "test-agent")
        csr = x509.load_pem_x509_csr(csr_pem)
        assert isinstance(csr.signature_hash_algorithm, hashes.SHA256)


# ---------------------------------------------------------------------------
# HTTP request
# ---------------------------------------------------------------------------


class TestRequestCertificate:
    @patch("stormpulse.enroll.urllib.request.urlopen")
    def test_happy_path(self, mock_urlopen: MagicMock) -> None:
        response_data = _mock_response()
        mock_urlopen.return_value = _mock_urlopen(response_data)

        result = request_certificate(
            "https://example.com/api/enroll/",
            "agent-1",
            "tok",
            b"CSR_PEM",
        )
        assert result["client_cert_pem"] == response_data["client_cert_pem"]
        assert result["ca_cert_pem"] == response_data["ca_cert_pem"]
        assert result["hmac_key"] == response_data["hmac_key"]
        assert result["dashboard_url"] == response_data["dashboard_url"]

    @patch("stormpulse.enroll.urllib.request.urlopen")
    def test_older_dashboard_without_url_remains_compatible(
        self,
        mock_urlopen: MagicMock,
    ) -> None:
        response_data = _mock_response()
        response_data.pop("dashboard_url")
        mock_urlopen.return_value = _mock_urlopen(response_data)

        result = request_certificate(
            "https://example.com/api/enroll/",
            "agent-1",
            "tok",
            b"CSR_PEM",
        )

        assert "dashboard_url" not in result

    @patch("stormpulse.enroll.urllib.request.urlopen")
    def test_invalid_dashboard_url_rejected(self, mock_urlopen: MagicMock) -> None:
        response_data = _mock_response()
        response_data["dashboard_url"] = "https://example.com/ws/pulse/"
        mock_urlopen.return_value = _mock_urlopen(response_data)

        with pytest.raises(EnrollError, match="invalid 'dashboard_url'"):
            request_certificate(
                "https://example.com/api/enroll/",
                "agent-1",
                "tok",
                b"CSR_PEM",
            )

    @patch("stormpulse.enroll.urllib.request.urlopen")
    def test_http_401_raises_with_hint(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "url",
            401,
            "Unauthorized",
            email.message.Message(),
            None,
        )
        with pytest.raises(EnrollError, match="single-use"):
            request_certificate(
                "https://example.com/api/enroll/",
                "a",
                "bad",
                b"csr",
            )

    @patch("stormpulse.enroll.urllib.request.urlopen")
    def test_connection_error_raises_with_hint(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        with pytest.raises(EnrollError, match="Is the dashboard running"):
            request_certificate(
                "https://example.com/api/enroll/",
                "a",
                "t",
                b"csr",
            )

    @patch("stormpulse.enroll.urllib.request.urlopen")
    def test_missing_field_raises(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen({"client_cert_pem": "x"})
        with pytest.raises(EnrollError, match="missing 'ca_cert_pem'"):
            request_certificate(
                "https://example.com/api/enroll/",
                "a",
                "t",
                b"csr",
            )

    @patch("stormpulse.enroll.urllib.request.urlopen")
    def test_invalid_json_raises(self, mock_urlopen: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not json"
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        with pytest.raises(EnrollError, match="correct enrollment URL"):
            request_certificate(
                "https://example.com/api/enroll/",
                "a",
                "t",
                b"csr",
            )


# ---------------------------------------------------------------------------
# Credential writing
# ---------------------------------------------------------------------------


class TestWriteCredentials:
    def test_creates_all_files(self, tmp_path: Path) -> None:
        creds = write_credentials(tmp_path / "creds", b"KEY_PEM", _mock_response())
        assert creds.client_cert.is_file()
        assert creds.client_key.is_file()
        assert creds.ca_cert.is_file()
        assert creds.hmac_key.is_file()

    def test_private_key_permissions(self, tmp_path: Path) -> None:
        creds = write_credentials(tmp_path / "creds", b"KEY_PEM", _mock_response())
        assert stat.S_IMODE(creds.client_key.stat().st_mode) == 0o640

    def test_hmac_key_permissions(self, tmp_path: Path) -> None:
        creds = write_credentials(tmp_path / "creds", b"KEY_PEM", _mock_response())
        assert stat.S_IMODE(creds.hmac_key.stat().st_mode) == 0o640

    def test_cert_permissions(self, tmp_path: Path) -> None:
        creds = write_credentials(tmp_path / "creds", b"KEY_PEM", _mock_response())
        assert stat.S_IMODE(creds.client_cert.stat().st_mode) == 0o644
        assert stat.S_IMODE(creds.ca_cert.stat().st_mode) == 0o644

    def test_directory_permissions(self, tmp_path: Path) -> None:
        creds_dir = tmp_path / "new_creds"
        write_credentials(creds_dir, b"KEY_PEM", _mock_response())
        assert stat.S_IMODE(creds_dir.stat().st_mode) == 0o700

    def test_preserves_existing_directory_permissions(self, tmp_path: Path) -> None:
        creds_dir = tmp_path / "creds"
        creds_dir.mkdir(mode=0o750)
        write_credentials(creds_dir, b"KEY_PEM", _mock_response())
        assert stat.S_IMODE(creds_dir.stat().st_mode) == 0o750

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        deep = tmp_path / "a" / "b" / "c"
        creds = write_credentials(deep, b"KEY_PEM", _mock_response())
        assert creds.client_key.is_file()

    def test_hmac_key_is_raw_bytes(self, tmp_path: Path) -> None:
        raw_hmac = b"x" * 32
        response = _mock_response(hmac_key=base64.b64encode(raw_hmac).decode())
        creds = write_credentials(tmp_path / "creds", b"KEY_PEM", response)
        assert creds.hmac_key.read_bytes() == raw_hmac

    def test_private_key_content(self, tmp_path: Path) -> None:
        key_pem = b"-----BEGIN PRIVATE KEY-----\nTEST\n-----END PRIVATE KEY-----\n"
        creds = write_credentials(tmp_path / "creds", key_pem, _mock_response())
        assert creds.client_key.read_bytes() == key_pem

    def test_refuses_overwrite_without_force(self, tmp_path: Path) -> None:
        creds_dir = tmp_path / "creds"
        write_credentials(creds_dir, b"KEY_PEM", _mock_response())
        with pytest.raises(EnrollError, match="already exist"):
            write_credentials(creds_dir, b"KEY_PEM_2", _mock_response())

    def test_allows_overwrite_with_force(self, tmp_path: Path) -> None:
        creds_dir = tmp_path / "creds"
        write_credentials(creds_dir, b"KEY_PEM_1", _mock_response())
        creds = write_credentials(creds_dir, b"KEY_PEM_2", _mock_response(), force=True)
        assert creds.client_key.read_bytes() == b"KEY_PEM_2"

    def test_invalid_base64_hmac_raises(self, tmp_path: Path) -> None:
        response = _mock_response(hmac_key="not-valid-base64!!!")
        with pytest.raises(EnrollError, match="invalid HMAC key"):
            write_credentials(tmp_path / "creds", b"KEY_PEM", response)


# ---------------------------------------------------------------------------
# Preflight writability check
# ---------------------------------------------------------------------------


class TestPreflightCredsDir:
    def test_creates_missing_dir_with_0o700(self, tmp_path: Path) -> None:
        target = tmp_path / "new_creds"
        preflight_creds_dir(target)
        assert target.is_dir()
        assert stat.S_IMODE(target.stat().st_mode) == 0o700

    def test_creates_parents(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b" / "c"
        preflight_creds_dir(target)
        assert target.is_dir()

    def test_leaves_marker_cleaned_up(self, tmp_path: Path) -> None:
        target = tmp_path / "creds"
        preflight_creds_dir(target)
        assert list(target.iterdir()) == []

    def test_preserves_existing_dir_permissions(self, tmp_path: Path) -> None:
        target = tmp_path / "creds"
        target.mkdir(mode=0o750)
        preflight_creds_dir(target)
        assert stat.S_IMODE(target.stat().st_mode) == 0o750

    def test_raises_when_parent_not_writable(self, tmp_path: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip("root bypasses permission bits")
        parent = tmp_path / "ro"
        parent.mkdir(mode=0o500)
        try:
            with pytest.raises(EnrollError, match="--creds-dir"):
                preflight_creds_dir(parent / "creds")
        finally:
            parent.chmod(0o700)  # so pytest can clean up

    def test_raises_when_existing_dir_not_writable(self, tmp_path: Path) -> None:
        if os.geteuid() == 0:
            pytest.skip("root bypasses permission bits")
        target = tmp_path / "creds"
        target.mkdir(mode=0o500)
        try:
            with pytest.raises(EnrollError, match="--creds-dir"):
                preflight_creds_dir(target)
        finally:
            target.chmod(0o700)


# ---------------------------------------------------------------------------
# CLI default creds-dir (euid-aware)
# ---------------------------------------------------------------------------


class TestDefaultCredsDir:
    def test_root_gets_etc_stormpulse(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from stormpulse.cli import _default_creds_dir

        monkeypatch.setattr(os, "geteuid", lambda: 0)
        assert _default_creds_dir() == "/etc/stormpulse"

    def test_user_gets_xdg_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from stormpulse.cli import _default_creds_dir

        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert _default_creds_dir() == str(tmp_path / "xdg" / "stormpulse")

    def test_user_falls_back_to_home_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        from stormpulse.cli import _default_creds_dir

        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert _default_creds_dir() == str(tmp_path / ".config" / "stormpulse")


# ---------------------------------------------------------------------------
# HTTP warnings
# ---------------------------------------------------------------------------


class TestHTTPWarning:
    @patch("stormpulse.enroll.urllib.request.urlopen")
    def test_http_endpoint_logs_warning(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen(_mock_response())
        with patch("stormpulse.enroll.logger") as mock_logger:
            request_certificate(
                "http://example.com/api/enroll/",
                "a",
                "t",
                b"csr",
            )
            mock_logger.warning.assert_called_once()
            assert "plain HTTP" in mock_logger.warning.call_args[0][0]

    @patch("stormpulse.enroll.urllib.request.urlopen")
    def test_https_endpoint_no_warning(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen(_mock_response())
        with patch("stormpulse.enroll.logger") as mock_logger:
            request_certificate(
                "https://example.com/api/enroll/",
                "a",
                "t",
                b"csr",
            )
            mock_logger.warning.assert_not_called()


# ---------------------------------------------------------------------------
# Enrollment metadata
# ---------------------------------------------------------------------------


class TestWriteEnrollMetadata:
    def test_writes_json(self, tmp_path: Path) -> None:
        creds_dir = tmp_path / "creds"
        creds_dir.mkdir()
        path = write_enroll_metadata(
            creds_dir,
            "https://example.com/api/enroll/",
            "agent-01",
        )
        data = json.loads(path.read_text())
        assert data["endpoint"] == "https://example.com/api/enroll/"
        assert data["agent_id"] == "agent-01"

    def test_writes_explicit_dashboard_url(self, tmp_path: Path) -> None:
        creds_dir = tmp_path / "creds"
        creds_dir.mkdir()
        path = write_enroll_metadata(
            creds_dir,
            "https://example.com/api/enroll/",
            "agent-01",
            "wss://pulse.example.com/ws/pulse/",
        )
        data = json.loads(path.read_text())
        assert data["dashboard_url"] == "wss://pulse.example.com/ws/pulse/"

    def test_permissions(self, tmp_path: Path) -> None:
        creds_dir = tmp_path / "creds"
        creds_dir.mkdir()
        path = write_enroll_metadata(creds_dir, "https://x/", "a")
        assert stat.S_IMODE(path.stat().st_mode) == 0o644

    def test_returns_path(self, tmp_path: Path) -> None:
        creds_dir = tmp_path / "creds"
        creds_dir.mkdir()
        path = write_enroll_metadata(creds_dir, "https://x/", "a")
        assert path == creds_dir / "enroll.json"

    def test_no_tmp_left(self, tmp_path: Path) -> None:
        creds_dir = tmp_path / "creds"
        creds_dir.mkdir()
        write_enroll_metadata(creds_dir, "https://x/", "a")
        assert not (creds_dir / "enroll.tmp").exists()


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------


class TestIntegration:
    @patch("stormpulse.enroll.urllib.request.urlopen")
    def test_full_enrollment_flow(
        self,
        mock_urlopen: MagicMock,
        tmp_path: Path,
    ) -> None:
        private_key, key_pem = generate_keypair()
        csr_pem = build_csr(private_key, "test-agent-01")

        csr = x509.load_pem_x509_csr(csr_pem)
        assert csr.is_signature_valid

        response_data = _mock_response()
        mock_urlopen.return_value = _mock_urlopen(response_data)

        server_response = request_certificate(
            "https://example.com/api/enroll/",
            "test-agent-01",
            "tok",
            csr_pem,
        )
        creds = write_credentials(tmp_path / "creds", key_pem, server_response)

        assert stat.S_IMODE(creds.client_key.stat().st_mode) == 0o640
        assert stat.S_IMODE(creds.hmac_key.stat().st_mode) == 0o640
        assert stat.S_IMODE(creds.client_cert.stat().st_mode) == 0o644
        assert stat.S_IMODE(creds.ca_cert.stat().st_mode) == 0o644

    @patch("stormpulse.enroll.urllib.request.urlopen")
    def test_private_key_never_in_request_body(
        self,
        mock_urlopen: MagicMock,
        tmp_path: Path,
    ) -> None:
        private_key, key_pem = generate_keypair()
        csr_pem = build_csr(private_key, "test-agent-02")

        response_data = _mock_response()
        mock_urlopen.return_value = _mock_urlopen(response_data)

        request_certificate(
            "https://example.com/api/enroll/",
            "test-agent-02",
            "tok",
            csr_pem,
        )

        call_args = mock_urlopen.call_args[0][0]
        request_body = json.loads(call_args.data)

        # CSR IS in the request body
        assert "BEGIN CERTIFICATE REQUEST" in request_body["csr_pem"]
        # Private key is NOT in the request body
        for value in request_body.values():
            assert "BEGIN PRIVATE KEY" not in str(value)


class TestEnrollCLI:
    def test_persists_dashboard_url_for_init(self, tmp_path: Path) -> None:
        from stormpulse.cli.enroll import cmd_enroll

        response = _mock_response()
        args = MagicMock(
            creds_dir=str(tmp_path / "creds"),
            endpoint="https://example.com/api/enroll/",
            agent_id="agent-01",
            token="one-time-token",
            force=False,
        )

        with (
            patch("stormpulse.enroll.preflight_creds_dir"),
            patch(
                "stormpulse.enroll.generate_keypair",
                return_value=(MagicMock(), b"KEY_PEM"),
            ),
            patch("stormpulse.enroll.build_csr", return_value=b"CSR_PEM"),
            patch(
                "stormpulse.enroll.request_certificate",
                return_value=response,
            ),
            patch(
                "stormpulse.enroll.write_credentials",
                return_value=MagicMock(),
            ),
            patch("stormpulse.enroll.write_enroll_metadata") as write_metadata,
        ):
            cmd_enroll(args)

        write_metadata.assert_called_once_with(
            tmp_path / "creds",
            "https://example.com/api/enroll/",
            "agent-01",
            "wss://pulse.example.com/ws/pulse/",
        )


# ---------------------------------------------------------------------------
# Renewal (CORE-010)
# ---------------------------------------------------------------------------


def _tls(creds: Path) -> TlsConfig:
    """The creds dir's paths, with the issuing CA already in ``ca.pem``."""
    if not (creds / "ca.pem").exists():
        (creds / "ca.pem").write_bytes(_CA_PEM)
    return TlsConfig(
        ca_cert=creds / "ca.pem",
        client_cert=creds / "agent.pem",
        client_key=creds / "agent-key.pem",
    )


_CA_NAME = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "ca")])
_CA_KEY = ec.generate_private_key(ec.SECP256R1())
_CA_PEM = (
    x509.CertificateBuilder()
    .subject_name(_CA_NAME)
    .issuer_name(_CA_NAME)
    .public_key(_CA_KEY.public_key())
    .serial_number(1)
    .not_valid_before(datetime.now(UTC) - timedelta(days=1))
    .not_valid_after(datetime.now(UTC) + timedelta(days=3650))
    .sign(_CA_KEY, hashes.SHA256())
    .public_bytes(serialization.Encoding.PEM)
)


def _sign(
    csr_pem: bytes,
    *,
    serial: int = 7,
    days: int = 90,
    start: timedelta = timedelta(0),
    ca_key: ec.EllipticCurvePrivateKey = _CA_KEY,
    cn: str | None = None,
) -> str:
    """Issue a cert for the CSR's key from the CA in every test's ``ca.pem``."""
    csr = x509.load_pem_x509_csr(csr_pem)
    subject = csr.subject
    if cn is not None:
        subject = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, cn)])
    now = datetime.now(UTC) + start
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(_CA_NAME)
        .public_key(csr.public_key())
        .serial_number(serial)
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=days))
        .sign(ca_key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


class _FakeRenewServer:
    """Stands in for urlopen: records each CSR, answers or fails on cue."""

    def __init__(self, *outcomes: Exception | None) -> None:
        self.outcomes = list(outcomes)
        self.csrs: list[x509.CertificateSigningRequest] = []
        self.calls: list[tuple[urllib.request.Request, dict[str, object]]] = []

    def __call__(self, req: urllib.request.Request, **kwargs: object) -> MagicMock:
        assert isinstance(req.data, bytes)
        body = json.loads(req.data)
        self.calls.append((req, kwargs))
        self.csrs.append(x509.load_pem_x509_csr(body["csr_pem"].encode()))
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome
        return _mock_urlopen(
            {"client_cert_pem": _sign(body["csr_pem"].encode()), "ca_cert_pem": "x"}
        )

    def public_keys(self) -> list[bytes]:
        return [
            c.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            for c in self.csrs
        ]


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://p/api/renew/", code, "x", email.message.Message(), None
    )


ENDPOINT = "https://pulse.example.com/api/renew/"
DASHBOARD = "wss://pulse.example.com/ws/pulse/"


class TestRenewCertificate:
    def test_happy_path_returns_cert_for_pending_key(self, tmp_path: Path) -> None:
        server = _FakeRenewServer()
        ctx = MagicMock()
        with patch("stormpulse.enroll.urllib.request.urlopen", side_effect=server):
            pem = renew_certificate(_tls(tmp_path), "agent-01", DASHBOARD, ctx)

        live_key = tmp_path / "agent-key.pem"
        key = serialization.load_pem_private_key(live_key.read_bytes(), password=None)
        cert = x509.load_pem_x509_certificate(pem)
        assert cert.public_key() == key.public_key()
        assert (tmp_path / "agent.pem").read_bytes() == pem
        assert stat.S_IMODE(live_key.stat().st_mode) == 0o600
        req, kwargs = server.calls[0]
        assert kwargs["context"] is ctx
        assert req.full_url == ENDPOINT
        assert isinstance(req.data, bytes)
        assert json.loads(req.data).keys() == {"csr_pem"}
        cn = server.csrs[0].subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
        assert cn[0].value == "agent-01"

    def test_lost_response_retry_sends_same_public_key(self, tmp_path: Path) -> None:
        server = _FakeRenewServer(urllib.error.URLError(TimeoutError("timed out")))
        with patch("stormpulse.enroll.urllib.request.urlopen", side_effect=server):
            with pytest.raises(RenewError) as exc_info:
                renew_certificate(_tls(tmp_path), "a", DASHBOARD, MagicMock())
            assert exc_info.value.reason == "unreachable"
            renew_certificate(_tls(tmp_path), "a", DASHBOARD, MagicMock())

        first, second = server.public_keys()
        assert first == second

    def test_pending_key_on_disk_before_request(self, tmp_path: Path) -> None:
        seen: list[bool] = []

        def urlopen(req: urllib.request.Request, **_: object) -> MagicMock:
            seen.append((tmp_path / "agent-key.pem.new").is_file())
            raise _http_error(404)

        with patch("stormpulse.enroll.urllib.request.urlopen", side_effect=urlopen):
            with pytest.raises(RenewError):
                renew_certificate(_tls(tmp_path), "a", DASHBOARD, MagicMock())
        assert seen == [True]

    def test_corrupt_pending_key_is_replaced(self, tmp_path: Path) -> None:
        (tmp_path / "agent-key.pem.new").write_bytes(b"garbage")
        server = _FakeRenewServer()
        with patch("stormpulse.enroll.urllib.request.urlopen", side_effect=server):
            renew_certificate(_tls(tmp_path), "a", DASHBOARD, MagicMock())
        assert b"BEGIN PRIVATE KEY" in (tmp_path / "agent-key.pem").read_bytes()

    def test_unwritable_pending_key_is_creds_not_writable(self, tmp_path: Path) -> None:
        (tmp_path / "agent-key.pem.new").mkdir()
        server = _FakeRenewServer()
        with patch("stormpulse.enroll.urllib.request.urlopen", side_effect=server):
            with pytest.raises(RenewError) as exc_info:
                renew_certificate(_tls(tmp_path), "a", DASHBOARD, MagicMock())
        assert exc_info.value.reason == "creds_not_writable"
        assert "sudo" not in str(exc_info.value)
        assert server.calls == []

    @pytest.mark.parametrize(
        ("code", "reason"),
        [
            (404, "endpoint_missing"),
            (403, "refused"),
            (401, "refused"),
            (500, "http_error"),
        ],
    )
    def test_http_errors_map_to_closed_reason(
        self, tmp_path: Path, code: int, reason: str
    ) -> None:
        server = _FakeRenewServer(_http_error(code))
        with patch("stormpulse.enroll.urllib.request.urlopen", side_effect=server):
            with pytest.raises(RenewError) as exc_info:
                renew_certificate(_tls(tmp_path), "a", DASHBOARD, MagicMock())
        assert exc_info.value.reason == reason
        assert exc_info.value.reason in RenewError.REASONS

    def test_socket_error_is_unreachable(self, tmp_path: Path) -> None:
        server = _FakeRenewServer(ConnectionResetError("reset"))
        with patch("stormpulse.enroll.urllib.request.urlopen", side_effect=server):
            with pytest.raises(RenewError) as exc_info:
                renew_certificate(_tls(tmp_path), "a", DASHBOARD, MagicMock())
        assert exc_info.value.reason == "unreachable"

    def test_cert_for_another_key_is_bad_response(self, tmp_path: Path) -> None:
        other_csr = build_csr(generate_keypair()[0], "a")
        resp = _mock_urlopen({"client_cert_pem": _sign(other_csr)})
        with patch("stormpulse.enroll.urllib.request.urlopen", return_value=resp):
            with pytest.raises(RenewError) as exc_info:
                renew_certificate(_tls(tmp_path), "a", DASHBOARD, MagicMock())
        assert exc_info.value.reason == "bad_response"

    @pytest.mark.parametrize(
        "payload", [{"ca_cert_pem": "x"}, {"client_cert_pem": "not a cert"}]
    )
    def test_malformed_response_is_bad_response(
        self, tmp_path: Path, payload: dict[str, str]
    ) -> None:
        with patch(
            "stormpulse.enroll.urllib.request.urlopen",
            return_value=_mock_urlopen(payload),
        ):
            with pytest.raises(RenewError) as exc_info:
                renew_certificate(_tls(tmp_path), "a", DASHBOARD, MagicMock())
        assert exc_info.value.reason == "bad_response"

    @pytest.mark.parametrize("raw", [b"<html>", b"[]"])
    def test_non_json_object_body_is_bad_response(
        self, tmp_path: Path, raw: bytes
    ) -> None:
        resp = _mock_urlopen({})
        resp.read.return_value = raw
        with patch("stormpulse.enroll.urllib.request.urlopen", return_value=resp):
            with pytest.raises(RenewError) as exc_info:
                renew_certificate(_tls(tmp_path), "a", DASHBOARD, MagicMock())
        assert exc_info.value.reason == "bad_response"

    def test_read_only_creds_dir_fails_up_front_writing_nothing(
        self, tmp_path: Path
    ) -> None:
        if os.geteuid() == 0:
            pytest.skip("root bypasses permission bits")
        creds = tmp_path / "creds"
        creds.mkdir()
        (creds / "agent.pem").write_bytes(b"live")
        tls = _tls(creds)
        creds.chmod(0o500)
        server = _FakeRenewServer()
        try:
            with (
                patch("stormpulse.enroll.urllib.request.urlopen", side_effect=server),
                patch("stormpulse.enroll.generate_keypair") as gen,
            ):
                with pytest.raises(RenewError) as exc_info:
                    renew_certificate(tls, "a", DASHBOARD, MagicMock())
        finally:
            creds.chmod(0o700)
        assert exc_info.value.reason == "creds_not_writable"
        gen.assert_not_called()
        assert server.calls == []
        assert sorted(p.name for p in creds.iterdir()) == ["agent.pem", "ca.pem"]

    def test_system_mode_fails_up_front_even_when_writable(
        self, tmp_path: Path
    ) -> None:
        """``sudo stormpulse renew`` would leave a root-only key (CORE-010 d1)."""
        server = _FakeRenewServer()
        with (
            patch("stormpulse.enroll.urllib.request.urlopen", side_effect=server),
            patch("stormpulse.enroll.detect_mode", return_value=InstallMode.SYSTEM),
            patch("stormpulse.enroll.generate_keypair") as gen,
        ):
            with pytest.raises(RenewError) as exc_info:
                renew_certificate(_tls(tmp_path), "a", DASHBOARD, MagicMock())
        assert exc_info.value.reason == "creds_not_writable"
        gen.assert_not_called()
        assert server.calls == []

    def test_request_uses_the_renew_timeout(self, tmp_path: Path) -> None:
        server = _FakeRenewServer()
        with (
            patch("stormpulse.enroll.urllib.request.urlopen", side_effect=server),
            patch("stormpulse.enroll.RENEW_TIMEOUT_SECONDS", 7.0),
        ):
            renew_certificate(_tls(tmp_path), "a", DASHBOARD, MagicMock())
        assert server.calls[0][1]["timeout"] == 7.0
        assert RENEW_TIMEOUT_SECONDS < 20  # under the ping timeout

    @pytest.mark.parametrize(
        ("kwargs", "why"),
        [
            ({"ca_key": ec.generate_private_key(ec.SECP256R1())}, "not signed"),
            ({"start": timedelta(days=-100)}, "validity window"),
            ({"start": timedelta(hours=1)}, "validity window"),
            ({"cn": "someone-else"}, "not issued to a"),
        ],
    )
    def test_cert_the_terminator_would_refuse_is_bad_response(
        self, tmp_path: Path, kwargs: dict[str, object], why: str
    ) -> None:
        old_cert, old_key = _seed_live_pair(tmp_path)

        def urlopen(req: urllib.request.Request, **_: object) -> MagicMock:
            assert isinstance(req.data, bytes)
            csr = json.loads(req.data)["csr_pem"].encode()
            return _mock_urlopen({"client_cert_pem": _sign(csr, **kwargs)})  # type: ignore[arg-type]

        with patch("stormpulse.enroll.urllib.request.urlopen", side_effect=urlopen):
            with pytest.raises(RenewError) as exc_info:
                renew_certificate(_tls(tmp_path), "a", DASHBOARD, MagicMock())
        assert exc_info.value.reason == "bad_response"
        assert why in str(exc_info.value)
        assert (tmp_path / "agent.pem").read_bytes() == old_cert
        assert (tmp_path / "agent-key.pem").read_bytes() == old_key

    def test_small_clock_skew_is_accepted(self, tmp_path: Path) -> None:
        def urlopen(req: urllib.request.Request, **_: object) -> MagicMock:
            assert isinstance(req.data, bytes)
            csr = json.loads(req.data)["csr_pem"].encode()
            pem = _sign(csr, start=timedelta(minutes=2))
            return _mock_urlopen({"client_cert_pem": pem})

        with patch("stormpulse.enroll.urllib.request.urlopen", side_effect=urlopen):
            renew_certificate(_tls(tmp_path), "a", DASHBOARD, MagicMock())

    def test_unreadable_ca_is_bad_response(self, tmp_path: Path) -> None:
        tls = _tls(tmp_path)
        tls.ca_cert.write_bytes(b"not a ca")
        with patch(
            "stormpulse.enroll.urllib.request.urlopen",
            side_effect=_FakeRenewServer(),
        ):
            with pytest.raises(RenewError) as exc_info:
                renew_certificate(tls, "a", DASHBOARD, MagicMock())
        assert exc_info.value.reason == "bad_response"


def _seed_live_pair(creds: Path, *, serial: int = 1) -> tuple[bytes, bytes]:
    """Write a matching live pair, as enrollment left it; return (cert, key)."""
    key, key_pem = generate_keypair()
    cert_pem = _sign(build_csr(key, "a"), serial=serial).encode()
    (creds / "agent.pem").write_bytes(cert_pem)
    (creds / "agent-key.pem").write_bytes(key_pem)
    return cert_pem, key_pem


def _renew(creds: Path) -> None:
    with patch(
        "stormpulse.enroll.urllib.request.urlopen", side_effect=_FakeRenewServer()
    ):
        renew_certificate(_tls(creds), "a", DASHBOARD, MagicMock())


def _cut_between_live_renames(creds: Path) -> None:
    """One renewal whose power is cut after the cert goes live, before the key."""
    real_replace = os.replace
    live = {creds / "agent.pem", creds / "agent-key.pem"}
    renamed: list[Path] = []

    def replace(src: str | Path, dst: str | Path) -> None:
        if Path(dst) in live:
            if renamed:
                raise OSError("power cut")
            renamed.append(Path(dst))
        real_replace(src, dst)

    with (
        patch(
            "stormpulse.enroll.urllib.request.urlopen",
            side_effect=_FakeRenewServer(),
        ),
        patch("stormpulse.enroll.os.replace", side_effect=replace),
    ):
        with pytest.raises(RenewError) as exc_info:
            renew_certificate(_tls(creds), "a", DASHBOARD, MagicMock())
    assert exc_info.value.reason == "creds_not_writable"


class TestInstallRenewedPair:
    def test_swap_leaves_live_pair_and_whole_prev_only(self, tmp_path: Path) -> None:
        old_cert, old_key = _seed_live_pair(tmp_path)
        _renew(tmp_path)

        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "agent-key.pem",
            "agent-key.pem.prev",
            "agent.pem",
            "agent.pem.prev",
            "ca.pem",
        ]
        assert (tmp_path / "agent.pem.prev").read_bytes() == old_cert
        assert (tmp_path / "agent-key.pem.prev").read_bytes() == old_key
        assert read_cert_serial(tmp_path / "agent.pem") == 7
        live = x509.load_pem_x509_certificate((tmp_path / "agent.pem").read_bytes())
        key = serialization.load_pem_private_key(
            (tmp_path / "agent-key.pem").read_bytes(), password=None
        )
        assert live.public_key() == key.public_key()

    def test_second_renew_keeps_one_prev_generation(self, tmp_path: Path) -> None:
        _seed_live_pair(tmp_path)
        _renew(tmp_path)
        middle = (tmp_path / "agent.pem").read_bytes()
        _renew(tmp_path)
        assert (tmp_path / "agent.pem.prev").read_bytes() == middle
        assert len(list(tmp_path.iterdir())) == 5

    def test_stale_prev_tmp_from_a_crash_is_cleared(self, tmp_path: Path) -> None:
        _seed_live_pair(tmp_path)
        (tmp_path / "agent.pem.prev.tmp").write_bytes(b"left by a crash")
        _renew(tmp_path)
        assert not (tmp_path / "agent.pem.prev.tmp").exists()
        assert read_cert_serial(tmp_path / "agent.pem") == 7

    def test_mismatched_live_pair_never_overwrites_prev(self, tmp_path: Path) -> None:
        old_cert, old_key = _seed_live_pair(tmp_path)
        (tmp_path / "agent.pem.prev").write_bytes(old_cert)
        (tmp_path / "agent-key.pem.prev").write_bytes(old_key)
        _, stray_key = generate_keypair()
        (tmp_path / "agent-key.pem").write_bytes(stray_key)  # crash mid-swap
        _renew(tmp_path)
        assert (tmp_path / "agent.pem.prev").read_bytes() == old_cert
        assert (tmp_path / "agent-key.pem.prev").read_bytes() == old_key

    def test_crash_between_live_renames_keeps_pending_key(self, tmp_path: Path) -> None:
        old_cert, old_key = _seed_live_pair(tmp_path)
        _cut_between_live_renames(tmp_path)

        new_cert = (tmp_path / "agent.pem").read_bytes()
        assert new_cert != old_cert
        assert (tmp_path / "agent-key.pem").read_bytes() == old_key
        assert (tmp_path / "agent-key.pem.new").exists()
        assert (tmp_path / "agent.pem.prev").read_bytes() == old_cert
        assert (tmp_path / "agent-key.pem.prev").read_bytes() == old_key

    def test_unwritable_new_cert_is_creds_not_writable(self, tmp_path: Path) -> None:
        old_cert, _ = _seed_live_pair(tmp_path)
        (tmp_path / "agent.pem.new").mkdir()
        with pytest.raises(RenewError) as exc_info:
            _renew(tmp_path)
        assert exc_info.value.reason == "creds_not_writable"
        assert "sudo" not in str(exc_info.value)
        assert (tmp_path / "agent.pem").read_bytes() == old_cert


class TestCompletePendingSwap:
    def test_rolls_an_interrupted_swap_forward(self, tmp_path: Path) -> None:
        _seed_live_pair(tmp_path)
        _cut_between_live_renames(tmp_path)
        pending = (tmp_path / "agent-key.pem.new").read_bytes()

        assert complete_pending_swap(_tls(tmp_path)) is True
        assert (tmp_path / "agent-key.pem").read_bytes() == pending
        assert not (tmp_path / "agent-key.pem.new").exists()
        assert presented_cert(_tls(tmp_path)) == tmp_path / "agent.pem"

    def test_pending_key_of_a_failed_attempt_is_left_alone(
        self, tmp_path: Path
    ) -> None:
        _, old_key = _seed_live_pair(tmp_path)
        with patch(
            "stormpulse.enroll.urllib.request.urlopen",
            side_effect=_FakeRenewServer(_http_error(404)),
        ):
            with pytest.raises(RenewError):
                renew_certificate(_tls(tmp_path), "a", DASHBOARD, MagicMock())

        assert complete_pending_swap(_tls(tmp_path)) is False
        assert (tmp_path / "agent-key.pem").read_bytes() == old_key
        assert (tmp_path / "agent-key.pem.new").exists()

    def test_no_pending_key_is_a_no_op(self, tmp_path: Path) -> None:
        _seed_live_pair(tmp_path)
        assert complete_pending_swap(_tls(tmp_path)) is False

    def test_unwritable_key_reports_false(self, tmp_path: Path) -> None:
        _seed_live_pair(tmp_path)
        _cut_between_live_renames(tmp_path)
        with patch("stormpulse.enroll.os.replace", side_effect=OSError("ro")):
            assert complete_pending_swap(_tls(tmp_path)) is False


class TestPresentedCert:
    def test_whole_live_pair_is_presented(self, tmp_path: Path) -> None:
        _seed_live_pair(tmp_path)
        assert presented_cert(_tls(tmp_path)) == tmp_path / "agent.pem"

    def test_broken_live_pair_presents_whole_prev(self, tmp_path: Path) -> None:
        _seed_live_pair(tmp_path)
        _renew(tmp_path)
        (tmp_path / "agent-key.pem").write_bytes(b"corrupt")
        assert presented_cert(_tls(tmp_path)) == tmp_path / "agent.pem.prev"

    def test_cut_swap_presents_live_it_will_roll_forward_to(
        self, tmp_path: Path
    ) -> None:
        _seed_live_pair(tmp_path)
        _cut_between_live_renames(tmp_path)
        assert presented_cert(_tls(tmp_path)) == tmp_path / "agent.pem"

    def test_nothing_whole_reports_live(self, tmp_path: Path) -> None:
        (tmp_path / "agent.pem").write_bytes(b"x")
        assert presented_cert(_tls(tmp_path)) == tmp_path / "agent.pem"


class TestRenewHelpers:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (
                "wss://pulse.example.com/ws/pulse/",
                "https://pulse.example.com/api/renew/",
            ),
            ("wss://p.example:8443/ws/pulse/?x=1", "https://p.example:8443/api/renew/"),
            ("ws://localhost:8000/ws/pulse/", "http://localhost:8000/api/renew/"),
        ],
    )
    def test_renew_endpoint_derives_from_transport(
        self, url: str, expected: str
    ) -> None:
        assert renew_endpoint(url) == expected

    def test_read_cert_not_after_and_serial(self, tmp_path: Path) -> None:
        key, _ = generate_keypair()
        path = tmp_path / "agent.pem"
        path.write_text(_sign(build_csr(key, "a"), serial=4242, days=30))
        not_after = read_cert_not_after(path)
        assert not_after is not None
        assert timedelta(days=29) < not_after - datetime.now(UTC) <= timedelta(days=30)
        assert read_cert_serial(path) == 4242

    def test_unreadable_cert_reads_as_none(self, tmp_path: Path) -> None:
        bad = tmp_path / "agent.pem"
        bad.write_bytes(b"nope")
        assert read_cert_not_after(bad) is None
        assert read_cert_serial(bad) is None
        assert read_cert_not_after(tmp_path / "missing.pem") is None

    def test_days_remaining_counts_whole_days(self) -> None:
        now = datetime(2026, 10, 5, tzinfo=UTC)
        assert days_remaining(now + timedelta(days=13, hours=23), now) == 13
        assert days_remaining(now + timedelta(days=14), now) == 14

    def test_pending_key_path_sits_beside_live_key(self) -> None:
        assert pending_key_path(Path("/c/agent-key.pem")) == Path(
            "/c/agent-key.pem.new"
        )


class TestRenewFileHygiene:
    """Hunted edges: key type, cert mode, and the enrollment error text."""

    def test_non_ec_pending_key_is_replaced_with_ec(self, tmp_path: Path) -> None:
        from cryptography.hazmat.primitives.asymmetric import rsa

        rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        (tmp_path / "agent-key.pem.new").write_bytes(
            rsa_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        server = _FakeRenewServer()
        with patch("stormpulse.enroll.urllib.request.urlopen", side_effect=server):
            renew_certificate(_tls(tmp_path), "a", DASHBOARD, MagicMock())
        assert isinstance(server.csrs[0].public_key(), ec.EllipticCurvePublicKey)
        live = serialization.load_pem_private_key(
            (tmp_path / "agent-key.pem").read_bytes(), password=None
        )
        assert isinstance(live, ec.EllipticCurvePrivateKey)

    def test_installed_cert_keeps_enrollment_mode(self, tmp_path: Path) -> None:
        _seed_live_pair(tmp_path)
        _renew(tmp_path)
        assert stat.S_IMODE((tmp_path / "agent.pem").stat().st_mode) == 0o644

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
    def test_permission_denied_pending_key_never_says_enroll_with_sudo(
        self, tmp_path: Path
    ) -> None:
        blocker = tmp_path / "agent-key.pem.tmp"  # _write_file's tmp for .new
        blocker.write_bytes(b"")
        blocker.chmod(0o400)
        server = _FakeRenewServer()
        with patch("stormpulse.enroll.urllib.request.urlopen", side_effect=server):
            with pytest.raises(RenewError) as exc_info:
                renew_certificate(_tls(tmp_path), "a", DASHBOARD, MagicMock())
        assert exc_info.value.reason == "creds_not_writable"
        assert "sudo" not in str(exc_info.value)
        assert "Permission denied" in str(exc_info.value)
        assert server.calls == []

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
    def test_permission_denied_new_cert_never_says_enroll_with_sudo(
        self, tmp_path: Path
    ) -> None:
        old_cert, _ = _seed_live_pair(tmp_path)
        blocker = tmp_path / "agent.pem.tmp"  # _write_file's tmp for agent.pem.new
        blocker.write_bytes(b"")
        blocker.chmod(0o400)
        with pytest.raises(RenewError) as exc_info:
            _renew(tmp_path)
        assert exc_info.value.reason == "creds_not_writable"
        assert "sudo" not in str(exc_info.value)
        assert "Permission denied" in str(exc_info.value)
        assert (tmp_path / "agent.pem").read_bytes() == old_cert
