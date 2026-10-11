"""The handshake: the ``register`` envelope sent on every fresh connection, and the ``register.ok`` that must answer it before any loop starts."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from websockets.asyncio.client import ClientConnection

from stormpulse import __version__
from stormpulse.agent.integrations_runtime import (
    build_integrations_payload,
    integration_components,
    replace_runtime_state,
)
from stormpulse.agent.metadata import build_commands_metadata
from stormpulse.agent.wire_contract import contract_digest, wire_contract_digest
from stormpulse.protocol import (
    SPEAKS,
    Component,
    Envelope,
    MessageType,
    ProtocolError,
    RegisterOkPayload,
    check_version,
    make_register,
)
from stormpulse.system_inventory import collect_system_inventory

if TYPE_CHECKING:
    from stormpulse.agent import Agent

logger = logging.getLogger(__name__)

#: How long a control plane has to answer ``register`` before the session is refused.
REGISTER_OK_TIMEOUT_SECONDS = 30


async def _discover_initial_state(agent: Agent) -> None:
    # CORE-005: discover initial state for each live Integration that declares a
    # discover capability and has no state yet (garage discovers here).
    for runtime in agent.integrations.values():
        if (
            runtime.status == "live"
            and runtime.descriptor.discover is not None
            and runtime.state is None
        ):
            discovered = await asyncio.to_thread(
                runtime.descriptor.discover, runtime.config
            )
            if discovered is not None:
                replace_runtime_state(runtime, discovered)


async def send_register(agent: Agent, ws: ClientConnection, url: str) -> str:
    """Build and send the register envelope after a fresh connection."""
    logger.info("Connected to dashboard at %s", url)

    await _discover_initial_state(agent)
    system_inventory = await asyncio.to_thread(collect_system_inventory)

    # Re-stat seal at register time so post-seal connects advertise promptly (ADR CORE-004).
    since = agent.signoff_state.unsealed_since()
    advertised_digest = contract_digest()
    register = make_register(
        agent.config.agent.id,
        __version__,
        agent.config.agent.pulse_token,
        # Both digests computed here, from the classes in this interpreter, so
        # the value is true of the agent that sent it (CORE-008 decision 4).
        # Never read from the published artifact: that file describes a commit.
        contract_digest=advertised_digest,
        wire_contract=wire_contract_digest(),
        commands=build_commands_metadata(agent.registry, agent.config.project),
        integrations=build_integrations_payload(agent.integrations),
        log_groups=sorted(agent.shippers),
        system_inventory=system_inventory,
        signoff_sealed=agent.signoff_state.is_sealed(),
        unsealed_since=since.isoformat() if since is not None else None,
        boot_id=agent.boot_id,
        agent_started_at=agent.started_at,
        components=[
            Component("stormpulse", __version__, ""),
            *integration_components(agent.integrations),
        ],
        # [] by construction: the session's JobManager is built after this frame
        # and dies with the socket (reconnect.py). Ids ride here once jobs outlive one.
        in_flight=[],
    )
    await ws.send(register.to_json())
    logger.info("Sent register (v%s)", __version__)
    if agent.pulse_logger is not None:
        agent.pulse_logger.info(
            "Connected to dashboard",
            "connection",
            {"url": url, "version": __version__},
        )
    return advertised_digest


def _refused(what: str) -> ProtocolError:
    """A handshake refusal naming both sides (CORE-011 d2: no silent downgrade)."""
    return ProtocolError(
        "Registration couldn't finish. Check the control-plane logs and protocol support. "
        "Upgrade the control plane before its agents, or restore the last compatible agent release. "
        f"{what}; this agent speaks {list(SPEAKS)}",
        reason="unsupported_version",
    )


async def await_register_ok(
    ws: ClientConnection, *, timeout: float = REGISTER_OK_TIMEOUT_SECONDS
) -> RegisterOkPayload:
    """Read the first inbound frame: a ``register.ok`` naming a spoken version, or a refusal.

    Anything else raises ``ProtocolError(reason="unsupported_version")``: a
    silence past *timeout*, an unreadable frame, an ``error`` (quoted), another
    type, a ``register.ok`` without ``protocol`` (a v1-only control plane), or
    one naming a version outside ``SPEAKS``.
    """
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout)
    except TimeoutError:
        raise _refused(f"no register.ok within {timeout:.0f}s") from None
    try:
        frame = Envelope.from_json(raw)
    except ProtocolError as exc:
        raise _refused(f"first frame refused ({exc.reason}): {exc}") from exc
    if frame.type is not MessageType.REGISTER_OK:
        detail = ""
        if frame.type is MessageType.ERROR:
            error = frame.payload.get("error")
            detail = (
                f" ({error[:500]!r})"
                if isinstance(error, str)
                else " (unreadable error response)"
            )
        raise _refused(f"first frame was {frame.type.value}{detail}, not register.ok")
    try:
        ok = RegisterOkPayload.from_dict(frame.payload)
    except ProtocolError as exc:
        raise _refused(f"register.ok names no protocol ({exc})") from exc
    check_version(ok.protocol)
    logger.info(
        "Registered on protocol v%d (control plane contract %s)",
        ok.protocol,
        ok.contract_digest,
    )
    return ok
