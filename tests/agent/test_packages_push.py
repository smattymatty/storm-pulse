"""The inventory.packages loop: sends at session start, then only when dpkg's
status file moves. A real temp file moved with ``os.utime``, never a faked stat."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from websockets.exceptions import ConnectionClosed

from stormpulse.agent import Agent, packages_push
from stormpulse.protocol import InventoryPackagesPayload, SourcePackage

_PAYLOAD = InventoryPackagesPayload(
    release="bookworm", packages=[SourcePackage(name="sqlite3", version="3.40.1-2")]
)


@pytest.fixture(autouse=True)
def fast_poll() -> Iterator[None]:
    original = packages_push.POLL_INTERVAL_SECONDS
    packages_push.POLL_INTERVAL_SECONDS = 0.02
    yield
    packages_push.POLL_INTERVAL_SECONDS = original


@pytest.fixture
def status(tmp_path: Path) -> Path:
    path = tmp_path / "status"
    path.write_text("", encoding="utf-8")
    os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    return path


def _sent(ws: AsyncMock) -> list[dict[str, Any]]:
    frames = [json.loads(c.args[0]) for c in ws.send.await_args_list]
    return [f for f in frames if f["type"] == "inventory.packages"]


async def _run(
    agent: Agent,
    ws: AsyncMock,
    status: Path,
    collect: Callable[[], InventoryPackagesPayload | None],
    during: Callable[[], None] = lambda: None,
) -> None:
    async def drive() -> None:
        await asyncio.sleep(0.07)
        during()
        await asyncio.sleep(0.1)
        agent.shutdown.set()

    await asyncio.gather(
        packages_push.packages_push_loop(
            agent, ws, status_path=status, collect=collect
        ),
        drive(),
    )


@pytest.mark.asyncio
async def test_sends_once_at_session_start_and_not_again_while_dpkg_is_still(
    agent: Agent, status: Path
) -> None:
    ws = AsyncMock()
    await _run(agent, ws, status, lambda: _PAYLOAD)
    sent = _sent(ws)
    assert len(sent) == 1
    assert sent[0]["payload"] == {
        "release": "bookworm",
        "packages": [{"name": "sqlite3", "version": "3.40.1-2"}],
    }


@pytest.mark.asyncio
async def test_a_moved_status_file_sends_once_more(agent: Agent, status: Path) -> None:
    ws = AsyncMock()
    await _run(
        agent,
        ws,
        status,
        lambda: _PAYLOAD,
        during=lambda: os.utime(status, ns=(2_000_000_000, 2_000_000_000)),
    )
    assert len(_sent(ws)) == 2


@pytest.mark.asyncio
async def test_a_failed_collect_sends_nothing_and_retries_at_the_next_poll(
    agent: Agent, status: Path
) -> None:
    results: list[InventoryPackagesPayload | None] = [None, None]
    ws = AsyncMock()
    await _run(agent, ws, status, lambda: results.pop(0) if results else _PAYLOAD)
    assert len(_sent(ws)) == 1


@pytest.mark.asyncio
async def test_a_failed_send_keeps_the_old_baseline(agent: Agent, status: Path) -> None:
    ws = AsyncMock()
    ws.send.side_effect = [RuntimeError("buffer full"), None, None]
    await _run(agent, ws, status, lambda: _PAYLOAD)
    assert ws.send.await_count == 2


@pytest.mark.asyncio
async def test_no_status_file_never_collects(agent: Agent, tmp_path: Path) -> None:
    calls: list[int] = []

    def collect() -> InventoryPackagesPayload | None:
        calls.append(1)
        return _PAYLOAD

    ws = AsyncMock()
    await _run(agent, ws, tmp_path / "absent", collect)
    assert calls == []
    ws.send.assert_not_called()


@pytest.mark.asyncio
async def test_dpkg_moving_during_the_collect_sends_again(
    agent: Agent, status: Path
) -> None:
    """The mtime is read before collecting: an apt run that lands mid-collect
    is caught at the next poll instead of hiding behind the newer mtime."""
    moves = [2_000_000_000]

    def collect() -> InventoryPackagesPayload | None:
        if moves:
            ns = moves.pop()
            os.utime(status, ns=(ns, ns))
        return _PAYLOAD

    ws = AsyncMock()
    await _run(agent, ws, status, collect)
    assert len(_sent(ws)) == 2


@pytest.mark.asyncio
async def test_a_closed_socket_ends_the_loop_so_the_session_reconnects(
    agent: Agent, status: Path
) -> None:
    ws = AsyncMock()
    ws.send.side_effect = ConnectionClosed(None, None)
    with pytest.raises(ConnectionClosed):
        await asyncio.wait_for(
            packages_push.packages_push_loop(
                agent, ws, status_path=status, collect=lambda: _PAYLOAD
            ),
            timeout=0.5,
        )


@pytest.mark.asyncio
async def test_collect_runs_off_the_event_loop(agent: Agent, status: Path) -> None:
    """dpkg-query blocks for seconds on a big host; it must not stall the loop."""
    loop_thread = threading.get_ident()
    threads: list[int] = []

    def collect() -> InventoryPackagesPayload | None:
        threads.append(threading.get_ident())
        return _PAYLOAD

    await _run(agent, AsyncMock(), status, collect)
    assert threads and loop_thread not in threads
