"""Tests for ``stormpulse renew`` (stormpulse.cli.renew, ADR CORE-010)."""

from __future__ import annotations

import argparse
import email.message
import json
import ssl
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from stormpulse.cli import main
from stormpulse.cli.renew import cmd_renew
from stormpulse.config import Config, ConfigError
from stormpulse.enroll import RENEW_TIMEOUT_SECONDS
from stormpulse.init.mode import InstallMode
from tests.helpers import AGENT_ID

URLOPEN = "stormpulse.enroll.urllib.request.urlopen"


_CA_NAME = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ca")])
_CA_KEY = ec.generate_private_key(ec.SECP256R1())


def _cert_pem(public_key: ec.EllipticCurvePublicKey, days: float) -> bytes:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, AGENT_ID)])
    now = datetime.now(UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(_CA_NAME)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=days, hours=1))
        .sign(_CA_KEY, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
    )


def _seed(config: Config, days: float) -> None:
    now = datetime.now(UTC)
    config.tls.ca_cert.write_bytes(
        x509.CertificateBuilder()
        .subject_name(_CA_NAME)
        .issuer_name(_CA_NAME)
        .public_key(_CA_KEY.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3650))
        .sign(_CA_KEY, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
    )
    key = ec.generate_private_key(ec.SECP256R1())
    config.tls.client_cert.write_bytes(_cert_pem(key.public_key(), days))
    config.tls.client_key.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


def _snapshot(d: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in d.iterdir() if p.is_file()}


class _Server:
    """Stands in for urlopen: 404 (no endpoint yet), or signs the CSR for 90 days."""

    def __init__(self, *, issue: bool) -> None:
        self.issue = issue
        self.calls: list[tuple[str, str, object, object]] = []

    def __call__(self, req: urllib.request.Request, **kwargs: object) -> MagicMock:
        assert isinstance(req.data, bytes)
        csr = x509.load_pem_x509_csr(json.loads(req.data)["csr_pem"].encode())
        cn = csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
        self.calls.append(
            (req.full_url, str(cn), kwargs.get("context"), kwargs.get("timeout"))
        )
        if not self.issue:
            raise urllib.error.HTTPError(
                req.full_url, 404, "x", email.message.Message(), None
            )
        pem = _cert_pem(csr.public_key(), 90).decode()  # type: ignore[arg-type]
        resp = MagicMock()
        resp.read.return_value = json.dumps({"client_cert_pem": pem}).encode()
        resp.__enter__.return_value = resp
        return resp


@pytest.fixture
def ctx(config: Config) -> Iterator[MagicMock]:
    """Wire cmd_renew to the tmp_path config and a stand-in TLS context."""
    sentinel = MagicMock(spec=ssl.SSLContext)
    with (
        patch("stormpulse.cli.renew.load_config", return_value=config),
        patch("stormpulse.cli.renew.create_ssl_context", return_value=sentinel),
    ):
        yield sentinel


def _run() -> None:
    cmd_renew(argparse.Namespace(config="/unused.toml"))


def test_404_exits_nonzero_one_line_only_pending_key_written(
    config: Config, ctx: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(config, days=10)
    before = _snapshot(config.tls.client_cert.parent)
    server = _Server(issue=False)
    with patch(URLOPEN, side_effect=server), pytest.raises(SystemExit) as exc:
        _run()

    assert isinstance(exc.value.code, str)  # sys.exit(str) exits 1
    assert exc.value.code.startswith("Renewal failed (endpoint_missing)")
    assert "\n" not in exc.value.code
    after = _snapshot(config.tls.client_cert.parent)
    pending = config.tls.client_key.name + ".new"
    assert set(after) - set(before) == {pending}
    assert {k: v for k, v in after.items() if k != pending} == before
    assert capsys.readouterr().out == (
        f"Client cert {config.tls.client_cert}: 10 days remaining\n"
    )


def test_success_ignores_window_and_prints_before_and_after(
    config: Config, ctx: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed(config, days=200)  # far outside T-30: the CLI renews anyway
    server = _Server(issue=True)
    # A distinct timeout, so a call that drops it (default 15s) is caught.
    with (
        patch(URLOPEN, side_effect=server),
        patch("stormpulse.enroll.RENEW_TIMEOUT_SECONDS", 7.0),
    ):
        _run()

    # Presents the current pair over mTLS, CN=agent id, under the ping timeout.
    assert server.calls == [
        (
            "http://localhost:0/api/renew/",
            config.agent.id,
            ctx,
            7.0,
        )
    ]
    assert RENEW_TIMEOUT_SECONDS < 20
    assert capsys.readouterr().out == (
        f"Client cert {config.tls.client_cert}: 200 days remaining\n"
        "Renewed: 90 days remaining\n"
    )
    prev = config.tls.client_cert.with_name(config.tls.client_cert.name + ".prev")
    assert prev.exists()


def test_config_error_exits_without_request(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch("stormpulse.cli.renew.load_config", side_effect=ConfigError("no file")),
        patch(URLOPEN) as urlopen,
        pytest.raises(SystemExit) as exc,
    ):
        _run()
    assert exc.value.code == "Renewal failed: no file"
    urlopen.assert_not_called()


def test_unloadable_pair_exits_without_request(config: Config) -> None:
    _seed(config, days=10)
    with (
        patch("stormpulse.cli.renew.load_config", return_value=config),
        patch(
            "stormpulse.cli.renew.create_ssl_context",
            side_effect=ssl.SSLError("bad pair"),
        ),
        patch(URLOPEN) as urlopen,
        pytest.raises(SystemExit) as exc,
    ):
        _run()
    assert isinstance(exc.value.code, str)
    assert exc.value.code.startswith("Renewal failed: ")
    urlopen.assert_not_called()


def test_sudo_on_a_system_node_exits_creds_not_writable(
    config: Config, ctx: MagicMock
) -> None:
    _seed(config, days=10)
    before = _snapshot(config.tls.client_cert.parent)
    with (
        patch("stormpulse.enroll.detect_mode", return_value=InstallMode.SYSTEM),
        patch(URLOPEN) as urlopen,
        pytest.raises(SystemExit) as exc,
    ):
        _run()
    assert isinstance(exc.value.code, str)
    assert exc.value.code.startswith("Renewal failed (creds_not_writable)")
    urlopen.assert_not_called()
    assert _snapshot(config.tls.client_cert.parent) == before


def test_unreadable_cert_prints_unknown_days(
    config: Config, ctx: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    config.tls.client_cert.write_bytes(b"not a cert")
    with patch(URLOPEN, side_effect=_Server(issue=False)), pytest.raises(SystemExit):
        _run()
    assert "unknown days remaining" in capsys.readouterr().out


def test_main_dispatches_renew_with_config_path() -> None:
    with (
        patch.object(sys, "argv", ["stormpulse", "renew", "/etc/x.toml"]),
        patch("stormpulse.cli.renew.cmd_renew") as cmd,
    ):
        main()
    cmd.assert_called_once()
    assert cmd.call_args.args[0].config == "/etc/x.toml"
