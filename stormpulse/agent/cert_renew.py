"""The agent renews its own client cert over mTLS (ADR CORE-010)."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from stormpulse import events
from stormpulse.agent.loops import sleep_or_shutdown
from stormpulse.agent.ssl_context import LoadedTls, load_tls_context
from stormpulse.enroll import (
    RENEW_WINDOW,
    URGENT_WINDOW,
    RenewError,
    days_remaining,
    read_cert_serial,
    renew_certificate,
)

if TYPE_CHECKING:
    from stormpulse.agent import Agent

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 86_400.0
_FAILED = "cert_renew_failed"


def _load(agent: Agent) -> LoadedTls:
    loaded = load_tls_context(agent.config.tls)
    agent._ssl_ctx = loaded.ctx
    return loaded


async def renew_and_reload(agent: Agent) -> LoadedTls:
    """One renewal off the event loop, then rebuild ``_ssl_ctx`` in memory.

    The live socket is left alone; the next reconnect presents the new cert
    (CORE-010 decision 3). Raises ``RenewError`` and leaves the context as is.
    """
    config = agent.config
    await asyncio.to_thread(
        renew_certificate,
        config.tls,
        config.agent.id,
        config.dashboard.url,
        agent._ssl_ctx,
    )
    return _load(agent)


def _reload(agent: Agent, previous: LoadedTls | None) -> LoadedTls | None:
    """Rebuild the context from disk; keep ``previous`` if the pair will not load."""
    cert = agent.config.tls.client_cert
    try:
        loaded = _load(agent)
    except OSError as exc:
        logger.error("Certificate %s will not load: %s", cert, exc)
        return previous
    if previous is not None and loaded.serial != previous.serial:
        logger.info("Certificate %s changed on disk; TLS context reloaded", cert)
    return loaded


async def cert_renew_loop(agent: Agent) -> None:
    """Check the cert once a day for the agent's whole life (CORE-010 decision 1).

    What loaded lives here, in memory; the daily sleep is the last-attempt
    clock, so reconnects never add attempts.
    """
    loaded: LoadedTls | None = None
    while not agent.shutdown.is_set():
        try:
            loaded = await check_cert(agent, loaded)
        except Exception:  # skylos: ignore - the agent outlives a renewal bug
            logger.exception("Certificate renewal check crashed; retrying tomorrow")
            events.emit(_FAILED, source="cert", reason="unspecified")
        if await sleep_or_shutdown(agent.shutdown, CHECK_INTERVAL_SECONDS):
            return


def _is_stale(agent: Agent, loaded: LoadedTls | None) -> bool:
    """True when the context may not hold the pair on disk (a renew by hand, a fix)."""
    if loaded is None or loaded.on_prev:
        return True
    return read_cert_serial(agent.config.tls.client_cert) != loaded.serial


async def check_cert(agent: Agent, loaded: LoadedTls | None) -> LoadedTls | None:
    """Reload a pair changed on disk, then renew from T-30 or off ``.prev``.

    Judges the cert the context presents, never the live file (decision 4).
    """
    if _is_stale(agent, loaded):
        loaded = _reload(agent, loaded)
    if loaded is None or loaded.not_after is None:
        logger.error("Cannot read the loaded client certificate; renewal check skipped")
        events.emit(_FAILED, source="cert", reason="cert_unreadable")
        return loaded
    remaining = loaded.not_after - datetime.now(UTC)
    days = days_remaining(loaded.not_after, datetime.now(UTC))
    serial = f"{loaded.serial:x}" if loaded.serial is not None else "unknown"
    logger.info(
        "Client certificate serial %s expires %s (%d days); renewal window opens %s",
        serial,
        f"{loaded.not_after:%Y-%m-%d}",
        days,
        f"{loaded.not_after - RENEW_WINDOW:%Y-%m-%d}",
    )
    if loaded.on_prev:
        logger.error(
            "Agent presents %s, %d days remaining; renewing now", loaded.cert, days
        )
    elif remaining >= RENEW_WINDOW:
        return loaded
    else:
        logger.info("Client certificate expires in %d days; renewing", days)
    return await _attempt(
        agent, loaded, days, urgent=loaded.on_prev or remaining < URGENT_WINDOW
    )


async def _attempt(
    agent: Agent, loaded: LoadedTls, days: int, *, urgent: bool
) -> LoadedTls:
    """One renewal, its journal line and its event; the pair in use afterwards."""
    try:
        renewed = await renew_and_reload(agent)
    except RenewError as exc:
        logger.log(
            logging.ERROR if urgent else logging.WARNING,
            "Certificate renewal failed (%s), %d days remaining: %s",
            exc.reason,
            days,
            exc,
        )
        events.emit(_FAILED, source="cert", days_remaining=days, reason=exc.reason)
        return loaded
    new_days = (
        days_remaining(renewed.not_after, datetime.now(UTC))
        if renewed.not_after
        else None
    )
    logger.info("Client certificate renewed; %s days remaining", new_days)
    events.emit("cert_renew_succeeded", source="cert", days_remaining=new_days)
    return renewed
