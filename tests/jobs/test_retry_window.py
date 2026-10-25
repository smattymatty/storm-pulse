"""The retry window (CORE-012 d3): when it opens, and whether a parked job's
next attempt is still admitted once it holds a permit again."""

from __future__ import annotations

import asyncio

import pytest

from stormpulse import events
from stormpulse.commands import jobs
from stormpulse.commands.jobs import JobManager, JobOutcome, ProgressCallback
from stormpulse.protocol import CancelReason, Envelope, MessageType
from tests.helpers import FakeWire
from tests.jobs.retry_support import (
    asks,
    park_behind_one_permit,
    results,
    settle,
    sleep_on,
)


def test_the_window_opens_at_the_first_permit_and_only_once(
    clock: list[float],
) -> None:
    """d3: time queued for a permit spends none of the window, a later attempt
    does not move it, and no declared window grants nothing."""
    clock[0] = 100.0
    window = jobs._RetryWindow(10)
    clock[0] = 150.0
    window.open()
    clock[0] = 155.0
    assert window.grants(5)
    assert not window.grants(6)
    window.open()
    assert not window.grants(6)
    unwindowed = jobs._RetryWindow(None)
    unwindowed.open()
    assert not unwindowed.grants(1)


@pytest.mark.asyncio
async def test_time_queued_for_a_permit_spends_none_of_the_window(
    wire: FakeWire, clock: list[float]
) -> None:
    """d3 at the manager: a job queued 100 s behind another still holds its whole
    60 s window when it first asks, so its 1 s park is granted."""
    mgr = JobManager("agent-1", wire.send)
    mgr._sem = asyncio.Semaphore(1)
    gate = asyncio.Event()
    calls = 0

    async def blocker(progress: ProgressCallback) -> JobOutcome:
        await gate.wait()
        clock[0] += 100
        return JobOutcome(success=True)

    async def queued(progress: ProgressCallback) -> JobOutcome:
        nonlocal calls
        calls += 1
        return asks() if calls == 1 else JobOutcome(success=True)

    mgr.start("req-block", "blocker", "demo", blocker)
    mgr.start("req-1", "wait_for_dep", "demo", queued, retry_window_s=60)
    for _ in range(3):
        await asyncio.sleep(0)
    gate.set()
    await settle(mgr)

    result = next(r for r in results(wire) if r.payload["request_id"] == "req-1")
    assert calls == 2, "the queued time was charged to the window"
    assert result.payload["attempts"] == 2
    assert result.payload["retries_exhausted"] is False
    assert result.payload["duration_ms"] == 0, "queued time counted as work"


@pytest.mark.asyncio
async def test_a_permit_freed_past_the_window_admits_no_attempt(
    wire: FakeWire, monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    """d3 at admission: the 1 s park ends inside the 2 s window, but another job
    holds the only permit until +6 s; the waiting attempt never runs."""
    p = park_behind_one_permit(monkeypatch, wire, clock)
    calls = 0

    async def handler(progress: ProgressCallback) -> JobOutcome:
        nonlocal calls
        calls += 1
        return asks() if calls == 1 else JobOutcome(success=True)

    async def blocker(progress: ProgressCallback) -> JobOutcome:
        p.held.set()
        await asyncio.sleep(0)
        clock[0] += 5
        return JobOutcome(success=True)

    p.mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=2)
    await p.parked.wait()
    p.mgr.start("req-block", "blocker", "demo", blocker)
    await settle(p.mgr)

    result = next(r for r in results(wire) if r.payload["request_id"] == "req-1")
    assert calls == 1, "an attempt started after the window closed"
    assert result.payload["success"] is False
    assert result.payload["attempts"] == 1
    assert result.payload["retries_exhausted"] is True
    assert result.payload["stderr"] == "dependency still loading"


@pytest.mark.asyncio
async def test_an_overslept_park_admits_no_attempt(
    wire: FakeWire, monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    """d3 at admission: after 1 s of work a 4 s park fits the 5 s window, but the
    loop wakes at +6 s; the second attempt, which would succeed, never runs."""

    async def oversleep(delay: float) -> None:
        clock[0] += delay + 1

    sleep_on(monkeypatch, oversleep)
    mgr = JobManager("agent-1", wire.send)
    calls = 0

    async def handler(progress: ProgressCallback) -> JobOutcome:
        nonlocal calls
        calls += 1
        clock[0] += 1
        return asks(after=4) if calls == 1 else JobOutcome(success=True)

    mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=5)
    await settle(mgr)

    assert calls == 1, "an attempt started after the window closed"
    (result,) = results(wire)
    payload = result.payload
    assert payload["success"] is False
    assert payload["attempts"] == 1
    assert payload["retries_exhausted"] is True
    assert payload["stderr"] == "dependency still loading"
    assert payload["duration_ms"] == 1000, "the overslept park counted as work"
    assert payload["error"]["code"] == "not_ready"


@pytest.mark.asyncio
async def test_a_park_ending_on_the_deadline_is_admitted(
    wire: FakeWire, monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    """d3: admission is inclusive like ``grants``; a second attempt that
    starts exactly at the deadline runs, and the 4 s park is not work."""

    async def exact(delay: float) -> None:
        clock[0] += delay

    sleep_on(monkeypatch, exact)
    mgr = JobManager("agent-1", wire.send)
    calls = 0

    async def handler(progress: ProgressCallback) -> JobOutcome:
        nonlocal calls
        calls += 1
        clock[0] += 1
        return asks(after=4) if calls == 1 else JobOutcome(success=True)

    mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=5)
    await settle(mgr)

    (result,) = results(wire)
    assert calls == 2
    assert result.payload["success"] is True
    assert result.payload["attempts"] == 2
    assert result.payload["duration_ms"] == 2000


@pytest.mark.asyncio
async def test_a_stop_while_queued_past_the_window_cancels_without_a_result(
    wire: FakeWire, monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    """d3 + shutdown: a job whose window closed while it queued waits for a
    permit to finish, so a stop in that wait cancels it and sends no result."""
    p = park_behind_one_permit(monkeypatch, wire, clock)

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return asks()

    async def blocker(progress: ProgressCallback) -> JobOutcome:
        clock[0] += 5
        p.held.set()
        await asyncio.Event().wait()
        return JobOutcome(success=True)

    p.mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=2)
    await p.parked.wait()
    p.mgr.start("req-block", "blocker", "demo", blocker)
    await p.woke.wait()
    assert results(wire) == [], "finished past the window without a permit"
    await p.mgr.shutdown_all(CancelReason.SHUTDOWN)

    assert results(wire) == []
    cancelled = [e for e in wire.sent if e.type is MessageType.COMMAND_CANCELLED]
    assert sorted(e.payload["request_id"] for e in cancelled) == ["req-1", "req-block"]
    assert p.mgr.load()["running"] == 0


@pytest.mark.asyncio
async def test_a_refusal_after_two_asks_reports_the_latest_one(
    wire: FakeWire, monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    """d3 at admission: the refused result carries the LAST real outcome, not the
    first; the second park oversleeps the 3 s window."""
    parks = 0

    async def park(delay: float) -> None:
        nonlocal parks
        parks += 1
        clock[0] += delay if parks == 1 else delay + 5

    sleep_on(monkeypatch, park)
    mgr = JobManager("agent-1", wire.send)
    calls = 0

    async def handler(progress: ProgressCallback) -> JobOutcome:
        nonlocal calls
        calls += 1
        return asks("first ask") if calls == 1 else asks("second ask")

    mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=3)
    await settle(mgr)

    (result,) = results(wire)
    assert calls == 2
    assert result.payload["attempts"] == 2
    assert result.payload["retries_exhausted"] is True
    assert result.payload["stderr"] == "second ask", "refusal reported a stale ask"


@pytest.mark.asyncio
async def test_a_refusal_records_the_last_attempts_concurrency(
    wire: FakeWire, monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    """The refused job_result's ``concurrent`` is the last attempt's, not the
    queue at refusal: a job spawned during the park does not inflate it."""
    release = asyncio.Event()
    mgr = JobManager("agent-1", wire.send)

    async def side(progress: ProgressCallback) -> JobOutcome:
        await release.wait()
        return JobOutcome(success=True)

    async def oversleep(delay: float) -> None:
        mgr.start("req-side", "side", "demo", side)
        clock[0] += delay + 5

    sleep_on(monkeypatch, oversleep)

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return asks()

    mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=2)
    events.buffer().drain("flush-before")
    await mgr._jobs["req-1"]
    release.set()
    await settle(mgr)

    (ev,) = [
        e
        for e in events.buffer().drain("capture")
        if e.get("kind") == "job_result" and e.get("command_ref") == "req-1"
    ]
    assert ev["retries_exhausted"] is True
    assert ev["concurrent"] == 1, "refusal counted a job spawned during the park"


@pytest.mark.asyncio
async def test_a_refused_attempt_sends_its_result_inside_a_permit(
    wire: FakeWire, monkeypatch: pytest.MonkeyPatch, clock: list[float]
) -> None:
    """d3 at admission: the refusal finishes inside the permit it waited for, as
    the terminal path does, so its admin-API result send counts as running."""

    async def oversleep(delay: float) -> None:
        clock[0] += delay + 5

    sleep_on(monkeypatch, oversleep)
    running_at_send: list[int] = []

    async def send(envelope: Envelope) -> None:
        if envelope.type is MessageType.COMMAND_RESULT:
            running_at_send.append(mgr.load()["running"])
        await wire.send(envelope)

    mgr = JobManager("agent-1", send)

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return asks()

    mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=2)
    await settle(mgr)

    (result,) = results(wire)
    assert result.payload["retries_exhausted"] is True
    assert running_at_send == [1], "the refusal finished without holding a permit"
