"""Tests for stormpulse.enroll."""

from __future__ import annotations

import base64
import email.message
import json
import os
import stat
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from stormpulse.enroll import (
    EnrollError,
    build_csr,
    generate_keypair,
    preflight_creds_dir,
    request_certificate,
    write_credentials,
    write_enroll_metadata,
)


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
            "https://example.com/api/enroll/", "agent-1", "tok", b"CSR_PEM",
        )
        assert result["client_cert_pem"] == response_data["client_cert_pem"]
        assert result["ca_cert_pem"] == response_data["ca_cert_pem"]
        assert result["hmac_key"] == response_data["hmac_key"]

    @patch("stormpulse.enroll.urllib.request.urlopen")
    def test_http_401_raises_with_hint(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "url", 401, "Unauthorized", email.message.Message(), None,
        )
        with pytest.raises(EnrollError, match="single-use"):
            request_certificate(
                "https://example.com/api/enroll/", "a", "bad", b"csr",
            )

    @patch("stormpulse.enroll.urllib.request.urlopen")
    def test_connection_error_raises_with_hint(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
        with pytest.raises(EnrollError, match="Is the dashboard running"):
            request_certificate(
                "https://example.com/api/enroll/", "a", "t", b"csr",
            )

    @patch("stormpulse.enroll.urllib.request.urlopen")
    def test_missing_field_raises(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen({"client_cert_pem": "x"})
        with pytest.raises(EnrollError, match="missing 'ca_cert_pem'"):
            request_certificate(
                "https://example.com/api/enroll/", "a", "t", b"csr",
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
                "https://example.com/api/enroll/", "a", "t", b"csr",
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
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        from stormpulse.cli import _default_creds_dir
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert _default_creds_dir() == str(tmp_path / "xdg" / "stormpulse")

    def test_user_falls_back_to_home_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
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
                "http://example.com/api/enroll/", "a", "t", b"csr",
            )
            mock_logger.warning.assert_called_once()
            assert "plain HTTP" in mock_logger.warning.call_args[0][0]

    @patch("stormpulse.enroll.urllib.request.urlopen")
    def test_https_endpoint_no_warning(self, mock_urlopen: MagicMock) -> None:
        mock_urlopen.return_value = _mock_urlopen(_mock_response())
        with patch("stormpulse.enroll.logger") as mock_logger:
            request_certificate(
                "https://example.com/api/enroll/", "a", "t", b"csr",
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
            creds_dir, "https://example.com/api/enroll/", "agent-01",
        )
        data = json.loads(path.read_text())
        assert data["endpoint"] == "https://example.com/api/enroll/"
        assert data["agent_id"] == "agent-01"

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
        self, mock_urlopen: MagicMock, tmp_path: Path,
    ) -> None:
        private_key, key_pem = generate_keypair()
        csr_pem = build_csr(private_key, "test-agent-01")

        csr = x509.load_pem_x509_csr(csr_pem)
        assert csr.is_signature_valid

        response_data = _mock_response()
        mock_urlopen.return_value = _mock_urlopen(response_data)

        server_response = request_certificate(
            "https://example.com/api/enroll/", "test-agent-01", "tok", csr_pem,
        )
        creds = write_credentials(tmp_path / "creds", key_pem, server_response)

        assert stat.S_IMODE(creds.client_key.stat().st_mode) == 0o640
        assert stat.S_IMODE(creds.hmac_key.stat().st_mode) == 0o640
        assert stat.S_IMODE(creds.client_cert.stat().st_mode) == 0o644
        assert stat.S_IMODE(creds.ca_cert.stat().st_mode) == 0o644

    @patch("stormpulse.enroll.urllib.request.urlopen")
    def test_private_key_never_in_request_body(
        self, mock_urlopen: MagicMock, tmp_path: Path,
    ) -> None:
        private_key, key_pem = generate_keypair()
        csr_pem = build_csr(private_key, "test-agent-02")

        response_data = _mock_response()
        mock_urlopen.return_value = _mock_urlopen(response_data)

        request_certificate(
            "https://example.com/api/enroll/", "test-agent-02", "tok", csr_pem,
        )

        call_args = mock_urlopen.call_args[0][0]
        request_body = json.loads(call_args.data)

        # CSR IS in the request body
        assert "BEGIN CERTIFICATE REQUEST" in request_body["csr_pem"]
        # Private key is NOT in the request body
        for value in request_body.values():
            assert "BEGIN PRIVATE KEY" not in str(value)
