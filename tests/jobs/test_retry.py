"""A job may ask to run again (CORE-012): the outcome's ask, and the manager
turning several attempts into one terminal result."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest

from stormpulse import events
from stormpulse.commands import jobs
from stormpulse.commands.jobs import JobManager, JobOutcome, ProgressCallback
from stormpulse.protocol import CancelReason, Envelope, FailureReason, MessageType
from tests.helpers import FakeWire


def test_an_outcome_asks_to_run_again_only_on_a_failure() -> None:
    asked = JobOutcome(success=False, stderr="not ready", retry_after_s=5)
    assert asked.retry_after_s == 5
    assert JobOutcome(success=False).retry_after_s is None
    with pytest.raises(ValueError, match="a success cannot ask to run again"):
        JobOutcome(success=True, retry_after_s=5)
    for bad in (0, -1, True, 2.5):
        with pytest.raises(ValueError, match="retry_after_s must be"):
            JobOutcome(success=False, retry_after_s=bad)  # type: ignore[arg-type]


def test_the_window_opens_at_the_first_permit_and_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """d3: time queued for a permit spends none of the window, a later attempt
    does not move it, and no declared window grants nothing."""
    clock = [100.0]
    monkeypatch.setattr(jobs, "time", SimpleNamespace(monotonic=lambda: clock[0]))
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
async def test_two_asks_then_a_success_is_one_result_after_three_attempts(
    wire: FakeWire,
) -> None:
    """d2: each park is a progress line naming the handler's reason; the control
    plane hears one command.result, carrying how many attempts it took."""
    mgr = JobManager("agent-1", wire.send)
    calls = 0

    async def handler(progress: ProgressCallback) -> JobOutcome:
        nonlocal calls
        calls += 1
        await progress("starting", 0, 1, "checking the local dependency")
        if calls < 3:
            return JobOutcome(
                success=False,
                exit_code=1,
                stderr="dependency still loading",
                failure_reason=FailureReason.OS_ERROR,
                retry_after_s=1,
            )
        return JobOutcome(success=True, stdout="ready")

    mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=60)
    await asyncio.gather(*mgr._jobs.values(), return_exceptions=True)

    results = [e for e in wire.sent if e.type is MessageType.COMMAND_RESULT]
    progress = [e for e in wire.sent if e.type is MessageType.COMMAND_PROGRESS]
    assert calls == 3
    assert len(results) == 1
    assert results[0].payload["success"] is True
    assert results[0].payload["attempts"] == 3
    assert [p.payload["stage"] for p in progress].count("starting") == 3
    parks = [p for p in progress if p.payload["stage"] != "starting"]
    assert len(parks) == 2
    assert all("dependency still loading" in p.payload["message"] for p in parks)


def _asks(stderr: str = "dependency still loading", after: int = 1) -> JobOutcome:
    return JobOutcome(
        success=False,
        exit_code=7,
        stderr=stderr,
        failure_reason=FailureReason.OS_ERROR,
        error_code="not_ready",
        retry_after_s=after,
    )


def _results(wire: FakeWire) -> list[Envelope]:
    return [e for e in wire.sent if e.type is MessageType.COMMAND_RESULT]


async def _settle(mgr: JobManager) -> None:
    await asyncio.gather(*mgr._jobs.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_a_job_that_never_asks_reports_one_attempt(wire: FakeWire) -> None:
    mgr = JobManager("agent-1", wire.send)

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return JobOutcome(success=True)

    mgr.start("req-1", "plain", "demo", handler)
    await _settle(mgr)

    (result,) = _results(wire)
    assert result.payload["attempts"] == 1
    assert result.payload["retries_exhausted"] is False
    (event,) = events.buffer().drain("t")
    assert event["attempts"] == 1
    assert "retries_exhausted" not in event


@pytest.mark.asyncio
async def test_a_delay_past_the_window_is_terminal_with_its_own_cause(
    wire: FakeWire,
) -> None:
    """d3: no attempt is parked past the window; the last outcome stands, marked."""
    mgr = JobManager("agent-1", wire.send)
    calls = 0

    async def handler(progress: ProgressCallback) -> JobOutcome:
        nonlocal calls
        calls += 1
        return _asks(after=10)

    mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=5)
    await _settle(mgr)

    assert calls == 1
    (result,) = _results(wire)
    payload = result.payload
    assert payload["success"] is False
    assert payload["attempts"] == 1
    assert payload["retries_exhausted"] is True
    assert payload["failure_reason"] == FailureReason.OS_ERROR
    assert payload["exit_code"] == 7
    assert payload["stderr"] == "dependency still loading"
    assert payload["error"]["code"] == "not_ready"
    (event,) = events.buffer().drain("t")
    assert event["retries_exhausted"] is True
    assert event["attempts"] == 1


@pytest.mark.asyncio
async def test_the_window_closing_mid_run_ends_the_attempts(wire: FakeWire) -> None:
    """A 2 s window holds one 1 s park; by the second ask less than 1 s is left."""
    mgr = JobManager("agent-1", wire.send)
    calls = 0

    async def handler(progress: ProgressCallback) -> JobOutcome:
        nonlocal calls
        calls += 1
        return _asks(stderr=f"still loading ({calls})")

    mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=2)
    await _settle(mgr)

    assert calls == 2
    (result,) = _results(wire)
    assert result.payload["attempts"] == 2
    assert result.payload["retries_exhausted"] is True
    assert result.payload["stderr"] == "still loading (2)"
    (event,) = events.buffer().drain("t")
    assert event["attempts"] == 2


@pytest.mark.asyncio
async def test_an_ask_without_a_declared_window_is_terminal(wire: FakeWire) -> None:
    mgr = JobManager("agent-1", wire.send)
    calls = 0

    async def handler(progress: ProgressCallback) -> JobOutcome:
        nonlocal calls
        calls += 1
        return _asks()

    mgr.start("req-1", "no_window", "demo", handler)
    await _settle(mgr)

    assert calls == 1
    (result,) = _results(wire)
    assert result.payload["attempts"] == 1
    assert result.payload["retries_exhausted"] is True


@pytest.mark.asyncio
async def test_a_parked_job_holds_no_permit(wire: FakeWire) -> None:
    """d4: while parked the job is pending, not running, and its permit serves
    another job; on_success fires once, and duration_ms sums both attempts' work
    (0.15 s each) but leaves out the 1 s park."""
    mgr = JobManager("agent-1", wire.send)
    mgr._sem = asyncio.Semaphore(1)
    seen: list[dict[str, int]] = []
    hooks = 0
    calls = 0

    async def parks(progress: ProgressCallback) -> JobOutcome:
        nonlocal calls
        calls += 1
        seen.append(mgr.load())
        await asyncio.sleep(0.15)
        return _asks() if calls == 1 else JobOutcome(success=True)

    async def on_success() -> None:
        nonlocal hooks
        hooks += 1

    async def other(progress: ProgressCallback) -> JobOutcome:
        return JobOutcome(success=True)

    mgr.start(
        "req-1",
        JobManager.QUOTA_COMMAND,
        "demo",
        parks,
        on_success=on_success,
        retry_window_s=60,
    )
    await asyncio.sleep(0.3)
    parked = mgr.load()
    assert mgr.active_count() == 1
    mgr.start("req-2", "other", "demo", other)
    await asyncio.wait_for(asyncio.shield(mgr._jobs["req-2"]), timeout=0.5)
    await _settle(mgr)

    assert parked["pending"] == 1
    assert parked["running"] == 0
    assert parked["quota_running"] == 0
    assert seen[0]["running"] == 1 and seen[0]["quota_running"] == 1
    assert seen[1]["running"] == 1 and seen[1]["quota_running"] == 1
    assert mgr.load()["running"] == 0
    assert hooks == 1
    first = next(r for r in _results(wire) if r.payload["request_id"] == "req-1")
    assert first.payload["attempts"] == 2
    assert 300 <= first.payload["duration_ms"] < 800


@pytest.mark.asyncio
async def test_a_later_attempt_that_raises_is_terminal(wire: FakeWire) -> None:
    mgr = JobManager("agent-1", wire.send)
    calls = 0

    async def handler(progress: ProgressCallback) -> JobOutcome:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _asks()
        raise RuntimeError("boom")

    mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=60)
    await _settle(mgr)

    (result,) = _results(wire)
    assert result.payload["failure_reason"] == FailureReason.OS_ERROR
    assert result.payload["attempts"] == 2
    assert result.payload["retries_exhausted"] is False


@pytest.mark.asyncio
async def test_a_stop_while_parked_cancels_without_a_result(wire: FakeWire) -> None:
    mgr = JobManager("agent-1", wire.send)

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return _asks(after=30)

    mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=60)
    await asyncio.sleep(0.05)
    assert mgr.is_running("req-1")
    await mgr.shutdown_all(CancelReason.SHUTDOWN)

    assert _results(wire) == []
    cancelled = [e for e in wire.sent if e.type is MessageType.COMMAND_CANCELLED]
    assert [e.payload["request_id"] for e in cancelled] == ["req-1"]
    assert mgr.load()["running"] == 0


@pytest.mark.asyncio
async def test_time_queued_for_a_permit_spends_none_of_the_window(
    wire: FakeWire, monkeypatch: pytest.MonkeyPatch
) -> None:
    """d3 at the manager: a job queued 100 s behind another still holds its whole
    60 s window when it first asks, so its 1 s park is granted."""
    clock = [0.0]
    monkeypatch.setattr(jobs, "time", SimpleNamespace(monotonic=lambda: clock[0]))
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
        return _asks() if calls == 1 else JobOutcome(success=True)

    mgr.start("req-block", "blocker", "demo", blocker)
    mgr.start("req-1", "wait_for_dep", "demo", queued, retry_window_s=60)
    for _ in range(3):
        await asyncio.sleep(0)
    gate.set()
    await _settle(mgr)

    result = next(r for r in _results(wire) if r.payload["request_id"] == "req-1")
    assert calls == 2, "the queued time was charged to the window"
    assert result.payload["attempts"] == 2
    assert result.payload["retries_exhausted"] is False
    assert result.payload["duration_ms"] == 0, "queued time counted as work"


@pytest.mark.asyncio
async def test_each_park_line_names_its_own_attempt(wire: FakeWire) -> None:
    """d2: a reader of the progress stream can tell the first park from the
    second, not just that something is still loading."""
    mgr = JobManager("agent-1", wire.send)
    calls = 0

    async def handler(progress: ProgressCallback) -> JobOutcome:
        nonlocal calls
        calls += 1
        return _asks() if calls < 3 else JobOutcome(success=True)

    mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=60)
    await _settle(mgr)

    parks = [
        e.payload["message"]
        for e in wire.sent
        if e.type is MessageType.COMMAND_PROGRESS
    ]
    assert len(parks) == 2
    assert re.search(r"\b1\b", parks[0]) and not re.search(r"\b2\b", parks[0])
    assert re.search(r"\b2\b", parks[1]) and not re.search(r"\b1\b", parks[1])


def _sleep_on(before: Callable[[float], Awaitable[None]]) -> Any:
    """Stand in for ``jobs.asyncio`` so a park is instant: ``before`` moves the
    clock (and may wait on the test) in place of the real ``asyncio.sleep``."""

    class _Asyncio:
        def __getattr__(self, name: str) -> Any:
            return getattr(asyncio, name)

        async def sleep(self, delay: float) -> None:
            await before(delay)

    return _Asyncio()


@pytest.mark.asyncio
async def test_a_permit_freed_past_the_window_admits_no_attempt(
    wire: FakeWire, monkeypatch: pytest.MonkeyPatch
) -> None:
    """d3 at admission: the 1 s park ends inside the 2 s window, but another job
    holds the only permit until +6 s; the waiting attempt never runs."""
    clock = [0.0]
    monkeypatch.setattr(jobs, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    parked, held = asyncio.Event(), asyncio.Event()

    async def park(delay: float) -> None:
        clock[0] += delay
        parked.set()
        await held.wait()

    monkeypatch.setattr(jobs, "asyncio", _sleep_on(park))
    mgr = JobManager("agent-1", wire.send)
    mgr._sem = asyncio.Semaphore(1)
    calls = 0

    async def handler(progress: ProgressCallback) -> JobOutcome:
        nonlocal calls
        calls += 1
        return _asks() if calls == 1 else JobOutcome(success=True)

    async def blocker(progress: ProgressCallback) -> JobOutcome:
        held.set()
        await asyncio.sleep(0)
        clock[0] += 5
        return JobOutcome(success=True)

    mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=2)
    await parked.wait()
    mgr.start("req-block", "blocker", "demo", blocker)
    await _settle(mgr)

    result = next(r for r in _results(wire) if r.payload["request_id"] == "req-1")
    assert calls == 1, "an attempt started after the window closed"
    assert result.payload["success"] is False
    assert result.payload["attempts"] == 1
    assert result.payload["retries_exhausted"] is True
    assert result.payload["stderr"] == "dependency still loading"


@pytest.mark.asyncio
async def test_an_overslept_park_admits_no_attempt(
    wire: FakeWire, monkeypatch: pytest.MonkeyPatch
) -> None:
    """d3 at admission: after 1 s of work a 4 s park fits the 5 s window, but the
    loop wakes at +6 s; the second attempt, which would succeed, never runs."""
    clock = [0.0]
    monkeypatch.setattr(jobs, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def oversleep(delay: float) -> None:
        clock[0] += delay + 1

    monkeypatch.setattr(jobs, "asyncio", _sleep_on(oversleep))
    mgr = JobManager("agent-1", wire.send)
    calls = 0

    async def handler(progress: ProgressCallback) -> JobOutcome:
        nonlocal calls
        calls += 1
        clock[0] += 1
        return _asks(after=4) if calls == 1 else JobOutcome(success=True)

    mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=5)
    await _settle(mgr)

    assert calls == 1, "an attempt started after the window closed"
    (result,) = _results(wire)
    payload = result.payload
    assert payload["success"] is False
    assert payload["attempts"] == 1
    assert payload["retries_exhausted"] is True
    assert payload["stderr"] == "dependency still loading"
    assert payload["duration_ms"] == 1000, "the overslept park counted as work"
    assert payload["error"]["code"] == "not_ready"


@pytest.mark.asyncio
async def test_a_park_ending_on_the_deadline_is_admitted(
    wire: FakeWire, monkeypatch: pytest.MonkeyPatch
) -> None:
    """d3: admission is inclusive like ``grants``; a second attempt that
    starts exactly at the deadline runs, and the 4 s park is not work."""
    clock = [0.0]
    monkeypatch.setattr(jobs, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def exact(delay: float) -> None:
        clock[0] += delay

    monkeypatch.setattr(jobs, "asyncio", _sleep_on(exact))
    mgr = JobManager("agent-1", wire.send)
    calls = 0

    async def handler(progress: ProgressCallback) -> JobOutcome:
        nonlocal calls
        calls += 1
        clock[0] += 1
        return _asks(after=4) if calls == 1 else JobOutcome(success=True)

    mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=5)
    await _settle(mgr)

    (result,) = _results(wire)
    assert calls == 2
    assert result.payload["success"] is True
    assert result.payload["attempts"] == 2
    assert result.payload["duration_ms"] == 2000


@pytest.mark.asyncio
async def test_a_stop_while_queued_past_the_window_cancels_without_a_result(
    wire: FakeWire, monkeypatch: pytest.MonkeyPatch
) -> None:
    """d3 + shutdown: a job whose window closed while it queued waits for a
    permit to finish, so a stop in that wait cancels it and sends no result."""
    clock = [0.0]
    monkeypatch.setattr(jobs, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    parked, held = asyncio.Event(), asyncio.Event()

    async def park(delay: float) -> None:
        clock[0] += delay
        parked.set()
        await held.wait()

    monkeypatch.setattr(jobs, "asyncio", _sleep_on(park))
    mgr = JobManager("agent-1", wire.send)
    mgr._sem = asyncio.Semaphore(1)

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return _asks()

    async def blocker(progress: ProgressCallback) -> JobOutcome:
        clock[0] += 5
        held.set()
        await asyncio.Event().wait()
        return JobOutcome(success=True)

    mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=2)
    await parked.wait()
    mgr.start("req-block", "blocker", "demo", blocker)
    for _ in range(5):
        await asyncio.sleep(0)
    assert _results(wire) == [], "finished past the window without a permit"
    await mgr.shutdown_all(CancelReason.SHUTDOWN)

    assert _results(wire) == []
    cancelled = [e for e in wire.sent if e.type is MessageType.COMMAND_CANCELLED]
    assert sorted(e.payload["request_id"] for e in cancelled) == ["req-1", "req-block"]
    assert mgr.load()["running"] == 0


@pytest.mark.asyncio
async def test_a_refusal_after_two_asks_reports_the_latest_one(
    wire: FakeWire, monkeypatch: pytest.MonkeyPatch
) -> None:
    """d3 at admission: the refused result carries the LAST real outcome, not the
    first; the second park oversleeps the 3 s window."""
    clock = [0.0]
    monkeypatch.setattr(jobs, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    parks = 0

    async def park(delay: float) -> None:
        nonlocal parks
        parks += 1
        clock[0] += delay if parks == 1 else delay + 5

    monkeypatch.setattr(jobs, "asyncio", _sleep_on(park))
    mgr = JobManager("agent-1", wire.send)
    calls = 0

    async def handler(progress: ProgressCallback) -> JobOutcome:
        nonlocal calls
        calls += 1
        return _asks("first ask") if calls == 1 else _asks("second ask")

    mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=3)
    await _settle(mgr)

    (result,) = _results(wire)
    assert calls == 2
    assert result.payload["attempts"] == 2
    assert result.payload["retries_exhausted"] is True
    assert result.payload["stderr"] == "second ask", "refusal reported a stale ask"


@pytest.mark.asyncio
async def test_a_refusal_records_the_last_attempts_concurrency(
    wire: FakeWire, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refused job_result's ``concurrent`` is the last attempt's, not the
    queue at refusal: a job spawned during the park does not inflate it."""
    clock = [0.0]
    monkeypatch.setattr(jobs, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    release = asyncio.Event()
    mgr = JobManager("agent-1", wire.send)

    async def side(progress: ProgressCallback) -> JobOutcome:
        await release.wait()
        return JobOutcome(success=True)

    async def oversleep(delay: float) -> None:
        mgr.start("req-side", "side", "demo", side)
        clock[0] += delay + 5

    monkeypatch.setattr(jobs, "asyncio", _sleep_on(oversleep))

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return _asks()

    mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=2)
    events.buffer().drain("flush-before")
    await mgr._jobs["req-1"]
    release.set()
    await _settle(mgr)

    (ev,) = [
        e
        for e in events.buffer().drain("capture")
        if e.get("kind") == "job_result" and e.get("command_ref") == "req-1"
    ]
    assert ev["retries_exhausted"] is True
    assert ev["concurrent"] == 1, "refusal counted a job spawned during the park"


@pytest.mark.asyncio
async def test_a_refused_attempt_sends_its_result_inside_a_permit(
    wire: FakeWire, monkeypatch: pytest.MonkeyPatch
) -> None:
    """d3 at admission: the refusal finishes inside the permit it waited for, as
    the terminal path does, so its admin-API result send counts as running."""
    clock = [0.0]
    monkeypatch.setattr(jobs, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def oversleep(delay: float) -> None:
        clock[0] += delay + 5

    monkeypatch.setattr(jobs, "asyncio", _sleep_on(oversleep))
    running_at_send: list[int] = []

    async def send(envelope: Envelope) -> None:
        if envelope.type is MessageType.COMMAND_RESULT:
            running_at_send.append(mgr.load()["running"])
        await wire.send(envelope)

    mgr = JobManager("agent-1", send)

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return _asks()

    mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=2)
    await _settle(mgr)

    (result,) = _results(wire)
    assert result.payload["retries_exhausted"] is True
    assert running_at_send == [1], "the refusal finished without holding a permit"
