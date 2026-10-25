"""Helpers shared by the ``test_retry*`` files: an asking outcome, the results
on the wire, and a park the test drives in place of ``asyncio.sleep``."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import pytest

from stormpulse.commands import jobs
from stormpulse.commands.jobs import JobManager, JobOutcome
from stormpulse.protocol import Envelope, FailureReason, MessageType
from tests.helpers import FakeWire


def asks(stderr: str = "dependency still loading", after: int = 1) -> JobOutcome:
    return JobOutcome(
        success=False,
        exit_code=7,
        stderr=stderr,
        failure_reason=FailureReason.OS_ERROR,
        error_code="not_ready",
        retry_after_s=after,
    )


def results(wire: FakeWire) -> list[Envelope]:
    return [e for e in wire.sent if e.type is MessageType.COMMAND_RESULT]


async def settle(mgr: JobManager) -> None:
    await asyncio.gather(*mgr._jobs.values(), return_exceptions=True)


def sleep_on(
    monkeypatch: pytest.MonkeyPatch, before: Callable[[float], Awaitable[None]]
) -> None:
    """Make a park instant: ``before`` moves the clock (and may wait on the test)
    in place of ``asyncio.sleep``, patched on ``jobs`` alone so the test's own
    ``await asyncio.sleep(0)`` still yields."""

    class _Asyncio:
        def __getattr__(self, name: str) -> Any:
            return getattr(asyncio, name)

        async def sleep(self, delay: float) -> None:
            await before(delay)

    monkeypatch.setattr(jobs, "asyncio", _Asyncio())


@dataclass(frozen=True, slots=True)
class Parked:
    mgr: JobManager
    parked: asyncio.Event
    held: asyncio.Event
    woke: asyncio.Event


def park_behind_one_permit(
    monkeypatch: pytest.MonkeyPatch, wire: FakeWire, clock: list[float]
) -> Parked:
    """A manager with one permit whose park moves the clock, sets ``parked``,
    waits for ``held`` (another job took the permit) and then sets ``woke``."""
    parked, held, woke = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def park(delay: float) -> None:
        clock[0] += delay
        parked.set()
        await held.wait()
        woke.set()

    sleep_on(monkeypatch, park)
    mgr = JobManager("agent-1", wire.send)
    mgr._sem = asyncio.Semaphore(1)
    return Parked(mgr, parked, held, woke)
