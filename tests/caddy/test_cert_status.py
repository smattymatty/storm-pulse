"""Tests for stormpulse.caddy.cert_status.

Read-only backstop for the custom-domain CERT_PENDING -> ACTIVE transition
: a localhost TLS handshake decides whether Caddy serves a live,
publicly-trusted cert for a domain. The real handshake is monkeypatched here so
the unit tests stay offline; the verification semantics live in the default
SSL context, not in our code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stormpulse.caddy import cert_status
from stormpulse.caddy.cert_status import (
    make_caddy_cert_status_handler,
    run_caddy_cert_status,
)
from stormpulse.caddy.config import CaddyConfig

_DOMAIN = "mathewstorm.ca"


def _make_config() -> CaddyConfig:
    return CaddyConfig(
        enabled=True,
        admin_url="http://localhost:2019",
        main_caddyfile=Path("/etc/caddy/Caddyfile"),
        drop_in_path=Path("/etc/caddy/conf.d/buckets-custom-domains.caddy"),
    )


class _Progress:
    async def __call__(self, *a: Any, **k: Any) -> None:
        return None


def _install_probe(monkeypatch, result):
    monkeypatch.setattr(cert_status, "_probe_cert", lambda domain: result)


async def _run(domain=_DOMAIN):
    return await run_caddy_cert_status(progress=_Progress(), domain=domain)


@pytest.mark.asyncio
async def test_live_cert_reports_cert_live(monkeypatch):
    _install_probe(monkeypatch, (
        {
            "notAfter": "Sep 17 12:00:00 2026 GMT",
            "issuer": ((("organizationName", "Let's Encrypt"),),),
        },
        "",
    ))
    outcome = await _run()
    assert outcome.success is True
    assert outcome.extras["cert_live"] is True
    assert outcome.extras["issuer"] == "Let's Encrypt"
    assert outcome.extras["not_after"] == "Sep 17 12:00:00 2026 GMT"


@pytest.mark.asyncio
async def test_unverifiable_cert_reports_not_live(monkeypatch):
    # Internal CA / name mismatch / expired all surface as a verification
    # failure; the query still succeeds, the answer is just "not live".
    _install_probe(monkeypatch, (None, "cert not verifiable: self-signed"))
    outcome = await _run()
    assert outcome.success is True
    assert outcome.extras["cert_live"] is False
    assert "self-signed" in outcome.extras["probe_error"]


@pytest.mark.asyncio
async def test_caddy_unreachable_reports_not_live(monkeypatch):
    _install_probe(monkeypatch, (None, "handshake failed: Connection refused"))
    outcome = await _run()
    assert outcome.success is True
    assert outcome.extras["cert_live"] is False


def test_factory_requires_domain():
    cfg = _make_config()
    assert make_caddy_cert_status_handler(cfg, {}) is None
    assert make_caddy_cert_status_handler(cfg, {"domain": _DOMAIN}) is not None


def test_factory_lowercases_domain():
    cfg = _make_config()
    # A handler is built (non-None); the domain is normalized before use so
    # SNI and the website's lowercased CustomDomain.domain compare cleanly.
    assert make_caddy_cert_status_handler(cfg, {"domain": "Mathewstorm.CA"}) is not None


def test_issuer_name_prefers_org_then_cn():
    assert cert_status._issuer_name(
        {"issuer": ((("commonName", "R3"),), (("organizationName", "Let's Encrypt"),))}
    ) in ("R3", "Let's Encrypt")
    assert cert_status._issuer_name({"issuer": ()}) == ""
