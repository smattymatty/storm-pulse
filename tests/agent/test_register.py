"""Tests for the initial register envelope sent right after a connection."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict, fields, replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from stormpulse import __version__
from stormpulse.agent import Agent
from stormpulse.agent.register import (
    REGISTER_OK_TIMEOUT_SECONDS,
    await_register_ok,
    send_register,
)
from stormpulse.agent.wire_contract import contract_digest, wire_contract_digest
from stormpulse.auth import NonceStore
from stormpulse.config import Config
from stormpulse.integrations import Freshness
from stormpulse.protocol import (
    SPEAKS,
    Component,
    MessageType,
    ProtocolError,
    RegisterOkPayload,
    RegisterPayload,
)
from stormpulse.signoff import SignoffState
from tests.helpers import (
    CONTRACT_DIGEST,
    SECRET,
    envelope,
    get_garage_state,
    make_fake_garage_state,
)


@pytest.mark.asyncio
async def test_send_register_sends_register_envelope(agent: Agent) -> None:
    """The first frame sent on a new connection is a register envelope.

    Carries the agent id, version, pulse token, and the resolved command
    catalogue. Every field is present (CORE-011 d7): with no [garage] and no
    shippers the empties are ``{}`` and ``[]``, never null, never absent.
    """
    ws = AsyncMock()

    with patch(
        "stormpulse.agent.register.collect_system_inventory",
        return_value={},
    ):
        await send_register(agent, ws, "wss://example.com/ws/")

    ws.send.assert_called_once()
    data = json.loads(ws.send.call_args[0][0])
    assert data["type"] == "register"
    assert data["agent_id"] == agent.config.agent.id
    payload = data["payload"]
    assert payload["pulse_token"] == agent.config.agent.pulse_token
    assert payload["version"]
    assert payload["integrations"] == {}
    assert payload["log_groups"] == []
    assert payload["system_inventory"] == {}
    assert "git_pull" in payload["commands"]
    # The whole declaration parses back as the class: nothing was left out.
    declared = RegisterPayload.from_dict(payload)
    assert asdict(declared) == payload
    assert set(payload) == {f.name for f in fields(RegisterPayload)}
    # With no Integration live, the agent is the one component it declares.
    assert declared.components == [Component("stormpulse", __version__, "")]


@pytest.mark.asyncio
async def test_send_register_carries_shipper_names_sorted_and_the_inventory(
    agent: Agent,
) -> None:
    """The non-empty halves of the declaration: ``log_groups`` is every shipper
    name in sorted order, whatever order they were built in, and
    ``system_inventory`` is what the collector returned, untouched."""
    agent.shippers = {
        "storage": MagicMock(),
        "pulse": MagicMock(),
        "caddy": MagicMock(),
    }
    inventory = {"os": {"id": "arch"}, "cpu": {"cores": 8}}
    ws = AsyncMock()
    with patch(
        "stormpulse.agent.register.collect_system_inventory", return_value=inventory
    ):
        await send_register(agent, ws, "wss://example.com/ws/")

    payload = json.loads(ws.send.call_args[0][0])["payload"]
    assert payload["log_groups"] == ["caddy", "pulse", "storage"]
    assert payload["system_inventory"] == inventory


@pytest.mark.asyncio
async def test_send_register_includes_garage_snapshot_when_enabled(
    agent_with_garage: Callable[..., Agent],
) -> None:
    """When [garage] is enabled the register carries a discovered snapshot."""
    fake_state = make_fake_garage_state()
    ag = agent_with_garage()
    ws = AsyncMock()

    with (
        patch("stormpulse.garage.discover.discover_garage", return_value=fake_state),
        patch("stormpulse.agent.register.collect_system_inventory", return_value={}),
    ):
        await send_register(ag, ws, "wss://example.com/ws/")

    data = json.loads(ws.send.call_args[0][0])
    garage_report = data["payload"]["integrations"]["garage"]
    assert garage_report["status"] == "live"
    assert garage_report["state"]["node_id"] == fake_state.node_id
    assert get_garage_state(ag) is fake_state
    # Two components (CORE-011 d7): the agent, then garage from the state just
    # discovered; an in-tree surface is never fenced and its digest is the pin's.
    assert data["payload"]["components"] == [
        {"name": "stormpulse", "version": __version__, "digest": ""},
        {"name": "garage", "version": fake_state.version, "digest": ""},
    ]
    assert garage_report["commands_fenced"] is None
    assert garage_report["command_specs_digest"].startswith("sha256:")


@pytest.mark.asyncio
async def test_send_register_folds_what_the_discovered_snapshot_says_of_itself(
    agent_with_garage: Callable[..., Agent],
) -> None:
    """CORE-011 d5: a discovery that skipped a bucket registers as incomplete, and
    the ids it read are owed to the first push; the blob itself repeats none of it."""
    discovered = replace(
        make_fake_garage_state(), read=Freshness(False, "t0", ("a" * 64,))
    )
    ag = agent_with_garage()
    ws = AsyncMock()
    with (
        patch("stormpulse.garage.discover.discover_garage", return_value=discovered),
        patch("stormpulse.agent.register.collect_system_inventory", return_value={}),
    ):
        await send_register(ag, ws, "wss://example.com/ws/")
    report = json.loads(ws.send.call_args[0][0])["payload"]["integrations"]["garage"]
    assert (report["complete"], report["last_full_read_at"]) == (False, "t0")
    assert report["changed_ids"] == ["a" * 64]
    assert "read" not in report["state"]


@pytest.mark.asyncio
async def test_send_register_survives_a_components_hook_of_the_wrong_shape(
    agent_with_garage: Callable[..., Agent],
) -> None:
    """A hook returning dicts instead of SdkComponents costs garage's entry, never
    the register: the frame still sends and lists the agent (CORE-005 d5)."""
    from dataclasses import replace

    ag = agent_with_garage()
    runtime = ag.integrations["garage"]
    runtime.descriptor = replace(
        runtime.descriptor,
        components=lambda c, s: ({"name": "garage", "version": "1"},),
    )
    ws = AsyncMock()
    with (
        patch(
            "stormpulse.garage.discover.discover_garage",
            return_value=make_fake_garage_state(),
        ),
        patch("stormpulse.agent.register.collect_system_inventory", return_value={}),
    ):
        await send_register(ag, ws, "wss://example.com/ws/")

    ws.send.assert_called_once()
    data = json.loads(ws.send.call_args[0][0])
    assert data["payload"]["components"] == [
        {"name": "stormpulse", "version": __version__, "digest": ""}
    ]


@pytest.mark.asyncio
async def test_send_register_reports_seal_state(
    config: Config,
    nonce_store: NonceStore,
    ssl_ctx: MagicMock,
    shutdown: asyncio.Event,
    tmp_path: Path,
) -> None:
    """The register frame advertises the agent's current seal state."""
    sealed_state = SignoffState(config.storage.db_path.parent)
    sealed_state.seal()
    try:
        ag = Agent(
            config,
            SECRET,
            nonce_store,
            ssl_ctx,
            shutdown,
            signoff_state=sealed_state,
        )
        ws = AsyncMock()

        with patch(
            "stormpulse.agent.register.collect_system_inventory",
            return_value={},
        ):
            await send_register(ag, ws, "wss://example.com/ws/")

        data = json.loads(ws.send.call_args[0][0])
        assert data["payload"]["signoff_sealed"] is True
        # When sealed there is no unsealed-since timestamp to advertise.
        assert data["payload"]["unsealed_since"] is None
    finally:
        sealed_state.unseal()


@pytest.mark.asyncio
async def test_send_register_advertises_unsealed_since(
    config: Config,
    nonce_store: NonceStore,
    ssl_ctx: MagicMock,
    shutdown: asyncio.Event,
) -> None:
    """An unsealed agent puts the wall-clock unsealed_since on the register.

    The dashboard uses this to render "unsealed for X" and to fire its
    "unsealed > N hours" pager without having to guess from its own
    register-history table - the agent owns the authoritative timestamp.
    """
    from datetime import datetime

    state = SignoffState(config.storage.db_path.parent)
    state.seal()
    state.unseal()  # now has a real unsealed_since marker
    ag = Agent(
        config,
        SECRET,
        nonce_store,
        ssl_ctx,
        shutdown,
        signoff_state=state,
    )
    ws = AsyncMock()

    with patch(
        "stormpulse.agent.register.collect_system_inventory",
        return_value={},
    ):
        await send_register(ag, ws, "wss://example.com/ws/")

    data = json.loads(ws.send.call_args[0][0])
    assert data["payload"]["signoff_sealed"] is False
    assert data["payload"]["unsealed_since"] is not None
    # Round-trips as a valid ISO timestamp.
    parsed = datetime.fromisoformat(data["payload"]["unsealed_since"])
    assert parsed.tzinfo is not None


@pytest.mark.asyncio
async def test_send_register_mirrors_to_pulse_logger(
    config: Config,
    nonce_store: NonceStore,
    ssl_ctx: MagicMock,
    shutdown: asyncio.Event,
) -> None:
    """A connect event is mirrored to the PulseLogger when one is configured."""
    pulse_logger = MagicMock()
    ag = Agent(
        config,
        SECRET,
        nonce_store,
        ssl_ctx,
        shutdown,
        signoff_state=SignoffState(config.storage.db_path.parent),
        pulse_logger=pulse_logger,
    )
    ws = AsyncMock()

    with patch(
        "stormpulse.agent.register.collect_system_inventory",
        return_value={},
    ):
        await send_register(ag, ws, "wss://test.example/ws/")

    pulse_logger.info.assert_called_once()
    args = pulse_logger.info.call_args
    assert args[0][0] == "Connected to dashboard"
    assert args[0][1] == "connection"
    assert args[0][2]["url"] == "wss://test.example/ws/"


# ---------------------------------------------------------------------------
# The handshake (CORE-011 d2): register advertises, register.ok picks, or refuses
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_register_advertises_speaks_and_both_digests(agent: Agent) -> None:
    """Every register names the versions spoken and the v2 digest of what this
    interpreter emits, beside the kept v1 ``wire_contract``."""
    ws = AsyncMock()
    with patch("stormpulse.agent.register.collect_system_inventory", return_value={}):
        await send_register(agent, ws, "wss://example.com/ws/")
    payload = json.loads(ws.send.call_args[0][0])["payload"]
    assert payload["speaks"] == list(SPEAKS)
    assert payload["contract_digest"] == contract_digest()
    assert payload["wire_contract"] == wire_contract_digest()
    assert payload["contract_digest"] != payload["wire_contract"]


@pytest.mark.asyncio
async def test_two_registers_in_one_process_carry_the_same_boot_identity(
    config: Config,
    nonce_store: NonceStore,
    ssl_ctx: MagicMock,
    shutdown: asyncio.Event,
    signoff_state: SignoffState,
    tmp_path: Path,
) -> None:
    """Boot id and process start are read once in ``Agent.__init__`` and repeated
    on every register of the process: a reconnect is told from a reboot and
    from a restart (CORE-011 d7)."""
    from datetime import datetime

    proc = tmp_path / "boot_id"
    proc.write_text("9c2d4f6e-1a3b-4c5d-8e7f-0a1b2c3d4e5f\n", encoding="utf-8")
    with patch("stormpulse.agent.identity.BOOT_ID_PATH", proc):
        ag = Agent(
            config, SECRET, nonce_store, ssl_ctx, shutdown, signoff_state=signoff_state
        )
    ws = AsyncMock()
    with (
        patch("stormpulse.agent.register.collect_system_inventory", return_value={}),
        patch("stormpulse.agent.identity.BOOT_ID_PATH", tmp_path / "rebooted"),
    ):
        await send_register(ag, ws, "wss://example.com/ws/")
        await send_register(ag, ws, "wss://example.com/ws/")
    first, second = (
        json.loads(call.args[0])["payload"] for call in ws.send.call_args_list
    )
    assert first["boot_id"] == "9c2d4f6e-1a3b-4c5d-8e7f-0a1b2c3d4e5f"
    assert second["boot_id"] == first["boot_id"]
    assert datetime.fromisoformat(first["agent_started_at"]).tzinfo is not None
    assert second["agent_started_at"] == first["agent_started_at"]


@pytest.mark.asyncio
async def test_send_register_declares_the_job_managers_in_flight_ids(
    agent: Agent,
) -> None:
    """``in_flight`` is read from the session's JobManager when one exists
    (ids only, CORE-011 d7) and is ``[]`` without one; the real session order
    is pinned in ``tests/agent/test_reconnect.py``."""
    ws = AsyncMock()
    with patch("stormpulse.agent.register.collect_system_inventory", return_value={}):
        await send_register(agent, ws, "wss://example.com/ws/")
        agent.job_manager = MagicMock(in_flight=lambda: ["req-7", "req-9"])
        await send_register(agent, ws, "wss://example.com/ws/")
    without, with_manager = (
        json.loads(call.args[0])["payload"] for call in ws.send.call_args_list
    )
    assert without["in_flight"] == []
    assert with_manager["in_flight"] == ["req-7", "req-9"]


def _ws_answering(*frames: str) -> AsyncMock:
    ws = AsyncMock()
    ws.recv = AsyncMock(side_effect=list(frames))
    return ws


def _register_ok(payload: dict[str, Any], *, v: int = SPEAKS[-1]) -> str:
    """A register.ok as raw JSON; ``v`` is set after the fact so an unspoken one builds."""
    frame = envelope(MessageType.REGISTER_OK, payload, agent_id="cp").to_dict()
    frame["v"] = v
    return json.dumps(frame)


@pytest.mark.asyncio
async def test_register_ok_naming_a_spoken_version_starts_the_session() -> None:
    ws = _ws_answering(
        _register_ok({"protocol": SPEAKS[-1], "contract_digest": CONTRACT_DIGEST})
    )
    ok = await await_register_ok(ws)
    assert ok == RegisterOkPayload(protocol=SPEAKS[-1], contract_digest=CONTRACT_DIGEST)
    ws.send.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_frame",
    [
        pytest.param(_register_ok({}), id="v1-only control plane: no protocol"),
        pytest.param(
            _register_ok({"server_name": "n", "server_id": 1}),
            id="today's register.ok",
        ),
        pytest.param(
            _register_ok({"protocol": 1, "contract_digest": CONTRACT_DIGEST}),
            id="protocol 1",
        ),
        pytest.param(
            _register_ok(
                {"protocol": SPEAKS[-1] + 1, "contract_digest": CONTRACT_DIGEST}
            ),
            id="protocol above",
        ),
        pytest.param(
            _register_ok({"protocol": True, "contract_digest": CONTRACT_DIGEST}),
            id="protocol bool",
        ),
        pytest.param(
            _register_ok({"protocol": "2", "contract_digest": CONTRACT_DIGEST}),
            id="protocol string",
        ),
        pytest.param(
            _register_ok({"protocol": 2.0, "contract_digest": CONTRACT_DIGEST}),
            id="protocol float",
        ),
        pytest.param(_register_ok({"protocol": SPEAKS[-1]}), id="no contract_digest"),
        pytest.param(
            _register_ok(
                {"protocol": SPEAKS[-1], "contract_digest": CONTRACT_DIGEST}, v=1
            ),
            id="register.ok in a v1 envelope",
        ),
        pytest.param(
            envelope(
                MessageType.ERROR,
                {"error": "bad_token", "type": "register"},
                agent_id="cp",
            ).to_json(),
            id="error frame",
        ),
        pytest.param(
            envelope(MessageType.HEARTBEAT_ACK, {}, agent_id="cp").to_json(),
            id="another type first",
        ),
        pytest.param("not json", id="unreadable"),
    ],
)
async def test_anything_but_a_spoken_register_ok_is_refused(first_frame: str) -> None:
    """One refusal, naming both sides, no silent downgrade; the socket is not written."""
    ws = _ws_answering(first_frame)
    with pytest.raises(ProtocolError) as info:
        await await_register_ok(ws)
    assert info.value.reason == "unsupported_version"
    assert str(info.value).endswith(f"this agent speaks {list(SPEAKS)}")
    ws.send.assert_not_called()
    ws.close.assert_not_called()


@pytest.mark.asyncio
async def test_refusal_of_an_error_frame_quotes_its_reason() -> None:
    """The control plane answers a bad token with ``error`` on an open socket."""
    ws = _ws_answering(
        envelope(
            MessageType.ERROR, {"error": "bad_token", "type": "register"}, agent_id="cp"
        ).to_json()
    )
    with pytest.raises(ProtocolError, match=r"first frame was error \('bad_token'\)"):
        await await_register_ok(ws)


@pytest.mark.asyncio
async def test_refusal_names_the_version_the_control_plane_picked() -> None:
    ws = _ws_answering(
        _register_ok({"protocol": 1, "contract_digest": CONTRACT_DIGEST})
    )
    with pytest.raises(ProtocolError, match=r"protocol version: 1; this agent speaks"):
        await await_register_ok(ws)


@pytest.mark.asyncio
async def test_silence_is_refused_after_the_timeout() -> None:
    async def never() -> str:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    ws = AsyncMock()
    ws.recv = never
    with pytest.raises(ProtocolError, match=r"no register.ok within 0s; this agent"):
        await await_register_ok(ws, timeout=0.01)
    assert REGISTER_OK_TIMEOUT_SECONDS == 30
