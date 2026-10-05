"""Tests for ``stormpulse.agent.cert_renew`` (ADR CORE-010)."""

from __future__ import annotations

import email.message
import json
import logging
import ssl
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from stormpulse import events
from stormpulse.agent import Agent
from stormpulse.agent.cert_renew import (
    check_cert,
    renew_and_reload,
)
from stormpulse.agent.ssl_context import LoadedTls, load_tls_context
from stormpulse.enroll import RenewError
from tests.helpers import AGENT_ID

URLOPEN = "stormpulse.enroll.urllib.request.urlopen"

_CA_NAME = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ca")])
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


def _issue(public_key: Any, days: float, serial: int, cn: str = AGENT_ID) -> bytes:
    """A cert from the CA in ``ca.pem``; one hour of slack so ``.days`` is exact."""
    now = datetime.now(UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
        .issuer_name(_CA_NAME)
        .public_key(public_key)
        .serial_number(serial)
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=days, hours=1))
        .sign(_CA_KEY, hashes.SHA256())
        .public_bytes(serialization.Encoding.PEM)
    )


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _write_pair(cert: Path, key: Path, days: float, serial: int) -> None:
    k = ec.generate_private_key(ec.SECP256R1())
    cert.write_bytes(_issue(k.public_key(), days, serial))
    key.write_bytes(_key_pem(k))


def _seed(agent: Agent, days: float, serial: int = 1) -> LoadedTls:
    """A live pair expiring in ``days``, loaded as the agent booted it."""
    tls = agent.config.tls
    tls.ca_cert.write_bytes(_CA_PEM)
    _write_pair(tls.client_cert, tls.client_key, days, serial)
    loaded = load_tls_context(tls)
    agent._ssl_ctx = loaded.ctx
    return loaded


def _prev(path: Path) -> Path:
    return path.with_name(path.name + ".prev")


class _Server:
    """Stands in for urlopen: 404 (no endpoint yet), or signs the CSR for 90 days."""

    def __init__(self, *, issue: bool = False) -> None:
        self.issue = issue
        self.calls = 0
        self.keys: list[bytes] = []

    def __call__(self, req: urllib.request.Request, **kwargs: object) -> MagicMock:
        self.calls += 1
        assert isinstance(req.data, bytes)
        csr = x509.load_pem_x509_csr(json.loads(req.data)["csr_pem"].encode())
        self.keys.append(
            csr.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        if not self.issue:
            raise urllib.error.HTTPError(
                req.full_url, 404, "x", email.message.Message(), None
            )
        pem = _issue(csr.public_key(), 90, 99).decode()
        resp = MagicMock()
        resp.read.return_value = json.dumps({"client_cert_pem": pem}).encode()
        resp.__enter__.return_value = resp
        return resp


@pytest.mark.asyncio
async def test_renew_rebuilds_context_from_installed_pair(agent: Agent) -> None:
    _seed(agent, 20)
    old_ctx = agent._ssl_ctx
    seen: dict[str, object] = {}

    def renew(*args: object, **kwargs: object) -> bytes:
        seen["args"], seen["kwargs"] = args, kwargs
        seen["thread"] = threading.current_thread()
        tls = agent.config.tls
        _write_pair(tls.client_cert, tls.client_key, 90, 42)
        return b"pem"

    with patch("stormpulse.agent.cert_renew.renew_certificate", side_effect=renew):
        loaded = await renew_and_reload(agent)

    assert loaded.serial == 42
    assert agent._ssl_ctx is loaded.ctx
    assert agent._ssl_ctx is not old_ctx
    assert seen["args"] == (
        agent.config.tls,
        agent.config.agent.id,
        agent.config.dashboard.url,
        old_ctx,
    )
    assert seen["kwargs"] == {}
    assert seen["thread"] is not threading.main_thread()


@pytest.mark.asyncio
async def test_failed_renew_keeps_loaded_context(agent: Agent) -> None:
    old_ctx = agent._ssl_ctx
    with (
        patch(
            "stormpulse.agent.cert_renew.renew_certificate",
            side_effect=RenewError("404", reason="endpoint_missing"),
        ),
        patch("stormpulse.agent.cert_renew.load_tls_context") as rebuild,
    ):
        with pytest.raises(RenewError):
            await renew_and_reload(agent)
    rebuild.assert_not_called()
    assert agent._ssl_ctx is old_ctx


# --- The daily loop (CORE-010 decisions 1 and 4) ---


def _cert_events() -> list[dict[str, Any]]:
    return [e for e in events.buffer().drain("t") if e["kind"].startswith("cert_")]


@pytest.mark.asyncio
@pytest.mark.parametrize(("days", "calls"), [(45, 0), (30, 0), (29, 1)])
async def test_attempts_only_inside_thirty_days(
    agent: Agent, days: int, calls: int
) -> None:
    loaded = _seed(agent, days)
    server = _Server()
    with patch(URLOPEN, side_effect=server):
        assert await check_cert(agent, loaded) is loaded
    assert server.calls == calls
    assert len(_cert_events()) == calls
    assert agent._ssl_ctx is loaded.ctx


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("days", "level"),
    [(29, logging.WARNING), (14, logging.WARNING), (13, logging.ERROR)],
)
async def test_404_is_a_logged_failure_error_inside_fourteen_days(
    agent: Agent, caplog: pytest.LogCaptureFixture, days: int, level: int
) -> None:
    loaded = _seed(agent, days)
    with patch(URLOPEN, side_effect=_Server()):
        with caplog.at_level(logging.INFO, logger="stormpulse.agent.cert_renew"):
            assert await check_cert(agent, loaded) is loaded
    failed = [r for r in caplog.records if "renewal failed" in r.getMessage()]
    assert [r.levelno for r in failed] == [level]
    assert [{k: v for k, v in e.items() if k != "ts"} for e in _cert_events()] == [
        {
            "source": "cert",
            "kind": "cert_renew_failed",
            "days_remaining": days,
            "reason": "endpoint_missing",
        }
    ]
    assert agent._ssl_ctx is loaded.ctx


@pytest.mark.asyncio
async def test_success_emits_days_and_no_key_or_cert_material(
    agent: Agent, caplog: pytest.LogCaptureFixture
) -> None:
    loaded = _seed(agent, 20, serial=1)
    with (
        patch(URLOPEN, side_effect=_Server(issue=True)),
        caplog.at_level(logging.DEBUG),
    ):
        renewed = await check_cert(agent, loaded)
    assert renewed is not None
    assert renewed.serial == 99
    assert agent._ssl_ctx is renewed.ctx
    evs = _cert_events()
    assert [(e["kind"], e["days_remaining"]) for e in evs] == [
        ("cert_renew_succeeded", 90)
    ]
    assert "BEGIN" not in json.dumps(evs)
    assert not [r for r in caplog.records if "BEGIN" in r.getMessage()]


@pytest.mark.asyncio
async def test_serial_changed_on_disk_rebuilds_context_without_request(
    agent: Agent,
) -> None:
    loaded = _seed(agent, 45, serial=4)
    tls = agent.config.tls
    _write_pair(tls.client_cert, tls.client_key, 45, 5)  # a by-hand renew
    server = _Server()
    with patch(URLOPEN, side_effect=server):
        reloaded = await check_cert(agent, loaded)
    assert reloaded is not None
    assert reloaded.serial == 5
    assert agent._ssl_ctx is reloaded.ctx
    assert agent._ssl_ctx is not loaded.ctx
    assert server.calls == 0


@pytest.mark.asyncio
async def test_same_serial_keeps_context(agent: Agent) -> None:
    loaded = _seed(agent, 45, serial=5)
    with patch("stormpulse.agent.cert_renew.load_tls_context") as rebuild:
        assert await check_cert(agent, loaded) is loaded
    rebuild.assert_not_called()
    assert agent._ssl_ctx is loaded.ctx


@pytest.mark.asyncio
async def test_changed_serial_that_will_not_load_keeps_context(agent: Agent) -> None:
    loaded = _seed(agent, 45, serial=5)
    with patch(
        "stormpulse.agent.cert_renew.load_tls_context",
        side_effect=ssl.SSLError("bad"),
    ):
        agent.config.tls.client_cert.write_bytes(b"garbage")
        assert await check_cert(agent, loaded) is loaded
    assert agent._ssl_ctx is loaded.ctx


@pytest.mark.asyncio
async def test_first_check_loads_what_the_agent_presents(agent: Agent) -> None:
    _seed(agent, 45, serial=8)
    loaded = await check_cert(agent, None)
    assert loaded is not None
    assert (loaded.serial, loaded.on_prev) == (8, False)
    assert agent._ssl_ctx is loaded.ctx


@pytest.mark.asyncio
async def test_nothing_loadable_emits_cert_unreadable(agent: Agent) -> None:
    agent.config.tls.client_cert.write_bytes(b"garbage")
    server = _Server()
    with patch(URLOPEN, side_effect=server):
        assert await check_cert(agent, None) is None
    assert server.calls == 0
    assert [e["reason"] for e in _cert_events()] == ["cert_unreadable"]


# --- The pair in use, not the live file (CORE-010 decisions 3 and 4) ---


def _boot_on_prev(agent: Agent, prev_days: float, live_days: float) -> LoadedTls:
    """A whole ``.prev`` and a live pair that will not load together."""
    tls = agent.config.tls
    _seed(agent, prev_days, serial=1)
    for live in (tls.client_cert, tls.client_key):
        _prev(live).write_bytes(live.read_bytes())
    stray = ec.generate_private_key(ec.SECP256R1())
    tls.client_cert.write_bytes(_issue(stray.public_key(), live_days, 2))
    loaded = load_tls_context(tls)
    assert loaded.on_prev
    agent._ssl_ctx = loaded.ctx
    return loaded


@pytest.mark.asyncio
async def test_running_on_prev_renews_now_and_warns_by_prev_expiry(
    agent: Agent, caplog: pytest.LogCaptureFixture
) -> None:
    loaded = _boot_on_prev(agent, prev_days=20, live_days=365)
    server = _Server()
    with patch(URLOPEN, side_effect=server), caplog.at_level(logging.INFO):
        assert (await check_cert(agent, loaded)) is not None
    assert server.calls == 1
    [ev] = _cert_events()
    assert (ev["kind"], ev["days_remaining"]) == ("cert_renew_failed", 20)
    failed = [r for r in caplog.records if "renewal failed" in r.getMessage()]
    assert [r.levelno for r in failed] == [logging.ERROR]


@pytest.mark.asyncio
async def test_running_on_prev_far_from_expiry_still_renews(agent: Agent) -> None:
    loaded = _boot_on_prev(agent, prev_days=200, live_days=365)
    server = _Server(issue=True)
    with patch(URLOPEN, side_effect=server):
        renewed = await check_cert(agent, loaded)
    assert server.calls == 1
    assert renewed is not None
    assert (renewed.serial, renewed.on_prev) == (99, False)


@pytest.mark.asyncio
async def test_interrupted_swap_rolls_forward_with_no_request(agent: Agent) -> None:
    """New cert live, old key live, pending key on disk: the cut decision 3 builds for."""
    tls = agent.config.tls
    loaded = _seed(agent, 20, serial=1)
    pending = ec.generate_private_key(ec.SECP256R1())
    for live in (tls.client_cert, tls.client_key):
        _prev(live).write_bytes(live.read_bytes())
    tls.client_cert.write_bytes(_issue(pending.public_key(), 90, 99))
    tls.client_key.with_name(tls.client_key.name + ".new").write_bytes(
        _key_pem(pending)
    )
    server = _Server()
    with patch(URLOPEN, side_effect=server):
        renewed = await check_cert(agent, loaded)
    assert server.calls == 0
    assert renewed is not None
    assert (renewed.serial, renewed.on_prev) == (99, False)
    assert agent._ssl_ctx is renewed.ctx
    assert tls.client_key.read_bytes() == _key_pem(pending)
    assert not tls.client_key.with_name(tls.client_key.name + ".new").exists()


@pytest.mark.asyncio
async def test_live_key_fixed_by_hand_leaves_prev_with_no_request(
    agent: Agent,
) -> None:
    tls = agent.config.tls
    _seed(agent, 200, serial=1)
    good_key = tls.client_key.read_bytes()
    for live in (tls.client_cert, tls.client_key):
        _prev(live).write_bytes(live.read_bytes())
    tls.client_key.write_bytes(b"corrupt")
    loaded = load_tls_context(tls)
    assert loaded.on_prev and loaded.serial == 1  # same serial as the live file
    tls.client_key.write_bytes(good_key)
    server = _Server()
    with patch(URLOPEN, side_effect=server):
        reloaded = await check_cert(agent, loaded)
    assert reloaded is not None
    assert reloaded.on_prev is False
    assert agent._ssl_ctx is reloaded.ctx
    assert server.calls == 0


@pytest.mark.asyncio
async def test_unreadable_live_key_falls_back_and_renews(agent: Agent) -> None:
    tls = agent.config.tls
    _seed(agent, 200, serial=1)
    for live in (tls.client_cert, tls.client_key):
        _prev(live).write_bytes(live.read_bytes())
    tls.client_key.write_bytes(b"root-owned, unreadable")
    server = _Server(issue=True)
    with patch(URLOPEN, side_effect=server):
        renewed = await check_cert(agent, None)
    assert server.calls == 1
    assert renewed is not None
    assert (renewed.serial, renewed.on_prev) == (99, False)
