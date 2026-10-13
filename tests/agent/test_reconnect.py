"""Tests for the outer reconnect loop in ``Agent.run``."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Coroutine, Iterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from websockets.exceptions import ConnectionClosed

from stormpulse import events
from stormpulse.agent import Agent
from stormpulse.protocol import SPEAKS, MessageType, ProtocolError
from tests.helpers import CONTRACT_DIGEST, envelope


@pytest.mark.asyncio
async def test_a_connection_error_is_one_reconnect_event_with_its_category(
    agent: Agent, shutdown: asyncio.Event
) -> None:
    """The flap is recorded while disconnected and ships on the next session;
    its three declared fields survive the registry's filter (CORE-011 d6)."""

    def refuse(*args: object, **kwargs: object) -> MagicMock:
        shutdown.set()
        raise OSError("Connection refused")

    with patch("stormpulse.agent.reconnect.connect", side_effect=refuse):
        await agent.run()

    flaps = [e for e in events.buffer().drain("b") if e["kind"] == "reconnect"]
    assert len(flaps) == 1
    assert flaps[0]["source"] == "ws"
    assert (flaps[0]["reason"], flaps[0]["category"]) == (
        "Connection error",
        "connection",
    )
    assert flaps[0]["error"] == "Connection refused"


@pytest.mark.asyncio
async def test_run_reconnects_on_connection_error(
    agent: Agent,
    shutdown: asyncio.Event,
) -> None:
    attempts = 0

    def mock_connect(*args: object, **kwargs: object) -> MagicMock:
        nonlocal attempts
        attempts += 1
        if attempts >= 3:
            shutdown.set()
        raise OSError("Connection refused")

    with patch("stormpulse.agent.reconnect.connect", side_effect=mock_connect):
        await agent.run()

    assert attempts >= 2


@pytest.mark.asyncio
async def test_run_exits_on_shutdown(
    agent: Agent,
    shutdown: asyncio.Event,
) -> None:
    shutdown.set()
    await agent.run()
    # Should exit immediately without attempting to connect.


@pytest.mark.asyncio
async def test_run_reconnects_on_connection_closed(
    agent: Agent,
    shutdown: asyncio.Event,
) -> None:
    attempts = 0

    def mock_connect(*a: object, **kw: object) -> MagicMock:
        nonlocal attempts
        attempts += 1
        if attempts >= 2:
            shutdown.set()
        raise ConnectionClosed(None, None)

    with patch("stormpulse.agent.reconnect.connect", side_effect=mock_connect):
        await agent.run()
    assert attempts >= 2


@pytest.mark.asyncio
async def test_run_handles_unexpected_exception(
    agent: Agent,
    shutdown: asyncio.Event,
) -> None:
    attempts = 0

    def mock_connect(*a: object, **kw: object) -> MagicMock:
        nonlocal attempts
        attempts += 1
        if attempts >= 1:
            shutdown.set()
        raise RuntimeError("totally unexpected")

    with patch("stormpulse.agent.reconnect.connect", side_effect=mock_connect):
        await agent.run()
    assert attempts >= 1


@pytest.mark.asyncio
async def test_backoff_resets_after_stable_session(
    agent: Agent,
    shutdown: asyncio.Event,
) -> None:
    """A drop after a stable session retries at reconnect_min_seconds.

    Real sessions always end via ConnectionClosed, so the clean-return
    reset never ran: attempts and delay accumulated forever and every
    blip paid the fully-clamped delay ("attempt 51, 445246s since first
    failure" - the 2026-07-09 deploy-blackout incident). A session that
    outlived reconnect_max_seconds starts a new failure era; an
    instantly-dying session must still back off (no hot loop).
    """
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    clock = [0.0]
    # Session lifetimes, in seconds: short, stable (>= max 0.2), short.
    durations = [0.01, 10.0, 0.01]
    recorded: list[float] = []

    class _FakeConn:
        def __init__(self, *a: object, **kw: object) -> None:
            pass

        async def __aenter__(self) -> MagicMock:
            return MagicMock()

        async def __aexit__(self, *exc: object) -> bool:
            return False

    async def fake_session(ag: object, ws: object, url: str) -> None:
        clock[0] += durations.pop(0)
        raise ConnectionClosed(None, None)

    async def fake_sleep(ag: object, delay: float) -> float:
        recorded.append(delay)
        if len(recorded) >= 3:
            shutdown.set()
        return min(delay * 1.5, 0.2)

    with (
        patch("stormpulse.agent.reconnect.connect", _FakeConn),
        patch("stormpulse.agent.reconnect._run_session", fake_session),
        patch("stormpulse.agent.reconnect._sleep_with_jitter", fake_sleep),
        patch(
            "stormpulse.agent.reconnect.time",
            SimpleNamespace(monotonic=lambda: clock[0]),
        ),
    ):
        await agent.run()

    # Short session backs off (0.05 -> 0.075); the stable session resets
    # the era, so its drop retries at min again instead of 0.075.
    assert recorded == pytest.approx([0.05, 0.05, 0.075])


# ---------------------------------------------------------------------------
# The handshake gates the session (CORE-011 d2): no register.ok, no loops
# ---------------------------------------------------------------------------


class _RecordingTaskGroup:  # skylos: ignore[SKY-Q702] a fake TaskGroup mirrors the real one's three methods; cohesion is the interface's, not ours
    """Stands in for ``asyncio.TaskGroup``: records what would run, runs nothing."""

    created: list[str] = []

    async def __aenter__(self) -> _RecordingTaskGroup:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def create_task(self, coro: Coroutine[Any, Any, None]) -> None:
        self.created.append(getattr(coro, "__qualname__", repr(coro)))
        coro.close()


def _ws_answering(first_frame: str) -> MagicMock:
    """One frame, then the peer hangs up: a loop that wrongly starts ends, never spins."""
    ws = MagicMock(send=AsyncMock(), close=AsyncMock())
    ws.recv = AsyncMock(side_effect=[first_frame, ConnectionClosed(None, None)])
    return ws


def _register_ok(payload: dict[str, Any]) -> str:
    return envelope(MessageType.REGISTER_OK, payload, agent_id="cp").to_json()


@pytest.fixture
def recorded_tasks() -> Iterator[list[str]]:
    _RecordingTaskGroup.created = []
    with (
        patch("asyncio.TaskGroup", _RecordingTaskGroup),
        patch(
            "stormpulse.agent.reconnect.send_register",
            AsyncMock(return_value=CONTRACT_DIGEST),
        ),
    ):
        yield _RecordingTaskGroup.created


@pytest.mark.asyncio
async def test_session_starts_the_loops_after_a_spoken_register_ok(
    agent: Agent, recorded_tasks: list[str]
) -> None:
    from stormpulse.agent.reconnect import _run_session

    ws = _ws_answering(
        _register_ok({"protocol": SPEAKS[-1], "contract_digest": CONTRACT_DIGEST})
    )
    await _run_session(agent, ws, "wss://cp/ws/")
    assert "receive_loop" in recorded_tasks
    assert "heartbeat_loop" in recorded_tasks
    assert agent.job_manager is None


@pytest.mark.asyncio
async def test_session_starts_one_spool_loop_per_declared_source(
    agent: Agent, recorded_tasks: list[str]
) -> None:
    from stormpulse.agent.reconnect import _run_session

    agent.event_spools = {"a": MagicMock(), "b": MagicMock()}
    ws = _ws_answering(
        _register_ok({"protocol": SPEAKS[-1], "contract_digest": CONTRACT_DIGEST})
    )
    await _run_session(agent, ws, "wss://cp/ws/")
    assert recorded_tasks.count("event_spool_loop") == 2


@pytest.mark.asyncio
async def test_session_sends_register_before_it_waits_for_the_answer(
    agent: Agent, recorded_tasks: list[str]
) -> None:
    """The control plane answers only a register it has read: waiting first
    is a 30 s refusal on every connect, and the loops never start."""
    from stormpulse.agent.reconnect import _run_session

    order: list[str] = []

    async def register(ag: object, ws: object, url: str) -> None:
        order.append("register")

    async def recv() -> str:
        order.append("recv")
        return _register_ok(
            {"protocol": SPEAKS[-1], "contract_digest": CONTRACT_DIGEST}
        )

    ws = MagicMock(send=AsyncMock(), close=AsyncMock())
    ws.recv = recv
    with patch("stormpulse.agent.reconnect.send_register", register):
        await _run_session(agent, ws, "wss://cp/ws/")
    assert order == ["register", "recv"]
    assert "receive_loop" in recorded_tasks


@pytest.mark.asyncio
async def test_register_declares_no_in_flight_jobs_by_session_order(
    agent: Agent,
) -> None:
    """The real register frame carries ``in_flight == []`` because the session's
    JobManager does not exist yet when it is sent and is gone when the session
    ends: the honest statement of "no job survives a socket" (CORE-011 d7).
    The day jobs outlive a reconnect, this test is the one that moves."""
    import json

    from stormpulse.agent.reconnect import _run_session

    manager_at_send: list[object] = []
    frames: list[dict[str, Any]] = []

    async def send(raw: str) -> None:
        manager_at_send.append(agent.job_manager)
        frames.append(json.loads(raw))

    ws = _ws_answering(
        _register_ok({"protocol": SPEAKS[-1], "contract_digest": CONTRACT_DIGEST})
    )
    ws.send = send
    with (
        patch("asyncio.TaskGroup", _RecordingTaskGroup),
        patch("stormpulse.agent.register.collect_system_inventory", return_value={}),
    ):
        await _run_session(agent, ws, "wss://cp/ws/")

    assert [f["type"] for f in frames] == ["register"]
    assert frames[0]["payload"]["in_flight"] == []
    assert manager_at_send == [None]
    assert agent.job_manager is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first_frame",
    [
        pytest.param(_register_ok({}), id="no protocol"),
        pytest.param(
            _register_ok({"protocol": 1, "contract_digest": CONTRACT_DIGEST}),
            id="protocol 1",
        ),
        pytest.param(
            envelope(
                MessageType.ERROR, {"error": "bad_token"}, agent_id="cp"
            ).to_json(),
            id="error frame",
        ),
    ],
)
async def test_a_refused_handshake_raises_before_any_loop_exists(
    agent: Agent, recorded_tasks: list[str], first_frame: str
) -> None:
    """A raise, never a return: a return would reset the backoff (reconnect.py)."""
    from stormpulse.agent.reconnect import _run_session

    with pytest.raises(ProtocolError) as info:
        await _run_session(agent, ws := _ws_answering(first_frame), "wss://cp/ws/")
    assert info.value.reason == "unsupported_version"
    assert recorded_tasks == []
    assert agent.job_manager is None
    ws.send.assert_not_called()


@pytest.mark.asyncio
async def test_a_refused_handshake_logs_one_warning_and_backs_off(
    agent: Agent,
    shutdown: asyncio.Event,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``run_with_backoff`` names both sides at warning, never error, and the delay grows."""
    recorded: list[float] = []

    @asynccontextmanager
    async def conn(*a: object, **kw: object) -> AsyncIterator[MagicMock]:
        yield _ws_answering(
            _register_ok({"protocol": 1, "contract_digest": CONTRACT_DIGEST})
        )

    async def fake_sleep(ag: object, delay: float) -> float:
        recorded.append(delay)
        if len(recorded) >= 2:
            shutdown.set()
        return delay * 1.5

    with (
        patch("stormpulse.agent.reconnect.connect", conn),
        patch(
            "stormpulse.agent.reconnect.send_register",
            AsyncMock(return_value=CONTRACT_DIGEST),
        ),
        patch("stormpulse.agent.reconnect._sleep_with_jitter", fake_sleep),
        caplog.at_level(logging.WARNING, logger="stormpulse.agent.reconnect"),
    ):
        await agent.run()

    refusals = [
        r for r in caplog.records if r.getMessage().startswith("Handshake refused")
    ]
    assert len(refusals) == 2
    assert all(r.levelno == logging.WARNING for r in refusals)
    assert (
        f"Unsupported protocol version: 1; this agent speaks {list(SPEAKS)}"
        in refusals[0].getMessage()
    )
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert recorded == pytest.approx([0.05, 0.075])


# ---------------------------------------------------------------------------
# log_enricher_provider (CORE-005 decision 13: enrichment keyed by parser)
# ---------------------------------------------------------------------------


def test_log_enricher_provider_is_tick_fresh(
    agent_with_garage,
) -> None:
    """The provider rebuilds from the declarer's CURRENT state on every call."""
    from stormpulse.agent.reconnect import log_enricher_provider
    from tests.helpers import (
        make_fake_garage_state,
        make_garage_bucket,
        set_garage_state,
    )

    ag = agent_with_garage()
    provider = log_enricher_provider(ag, "garage_s3")

    bucket_id = "c" * 64
    state = make_fake_garage_state().with_items(
        [make_garage_bucket(bucket_id, alias="my-bucket")]
    )
    set_garage_state(ag, state)
    enricher = provider()
    assert enricher is not None
    assert enricher("GKkey", "my-bucket") == bucket_id

    # State changes; the NEXT provider() call must see it, not a frozen snapshot.
    set_garage_state(ag, None)
    enricher = provider()
    assert enricher is not None
    assert enricher("GKkey", "my-bucket") == ""


def test_log_enricher_provider_none_for_undeclared_parser(
    agent_with_garage,
) -> None:
    """A parser no Integration declares yields None: the shipper skips stamping."""
    from stormpulse.agent.reconnect import log_enricher_provider

    ag = agent_with_garage()
    assert log_enricher_provider(ag, "raw")() is None


# ---------------------------------------------------------------------------
# A graceful stop cancels the jobs before the socket closes (CORE-011 d9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_watcher_cancels_the_jobs_with_the_reason_then_closes(
    agent: Agent, shutdown: asyncio.Event
) -> None:
    """The order is the whole point: a ``command.cancelled`` needs the socket,
    so the jobs are cancelled and told first, the close comes last."""
    from stormpulse.agent.reconnect import _shutdown_watcher
    from stormpulse.protocol import CancelReason

    order: list[object] = []
    agent.job_manager = MagicMock(
        shutdown_all=AsyncMock(side_effect=lambda reason: order.append(reason))
    )
    ws = MagicMock(close=AsyncMock(side_effect=lambda: order.append("close")))
    shutdown.set()
    await _shutdown_watcher(agent, ws)
    assert order == [CancelReason.SHUTDOWN, "close"]

    # No manager (the session never got one): the close alone.
    order.clear()
    agent.job_manager = None
    await _shutdown_watcher(agent, ws)
    assert order == ["close"]
