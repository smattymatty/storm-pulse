"""A job may ask to run again (CORE-012): the outcome's ask, and the manager
turning several attempts into one terminal result."""

from __future__ import annotations

import asyncio
import re

import pytest

from stormpulse import events
from stormpulse.commands.jobs import JobManager, JobOutcome, ProgressCallback
from stormpulse.protocol import CancelReason, FailureReason, MessageType
from tests.helpers import FakeWire
from tests.jobs.retry_support import asks, results, settle, sleep_on


def test_an_outcome_asks_to_run_again_only_on_a_failure() -> None:
    asked = JobOutcome(success=False, stderr="not ready", retry_after_s=5)
    assert asked.retry_after_s == 5
    assert JobOutcome(success=False).retry_after_s is None
    with pytest.raises(ValueError, match="a success cannot ask to run again"):
        JobOutcome(success=True, retry_after_s=5)
    for bad in (0, -1, True, 2.5):
        with pytest.raises(ValueError, match="retry_after_s must be"):
            JobOutcome(success=False, retry_after_s=bad)  # type: ignore[arg-type]


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

    (result,) = results(wire)
    progress = [e for e in wire.sent if e.type is MessageType.COMMAND_PROGRESS]
    assert calls == 3
    assert result.payload["success"] is True
    assert result.payload["attempts"] == 3
    assert [p.payload["stage"] for p in progress].count("starting") == 3
    parks = [p for p in progress if p.payload["stage"] != "starting"]
    assert len(parks) == 2
    assert all("dependency still loading" in p.payload["message"] for p in parks)


@pytest.mark.asyncio
async def test_a_job_that_never_asks_reports_one_attempt(wire: FakeWire) -> None:
    mgr = JobManager("agent-1", wire.send)

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return JobOutcome(success=True)

    mgr.start("req-1", "plain", "demo", handler)
    await settle(mgr)

    (result,) = results(wire)
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
        return asks(after=10)

    mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=5)
    await settle(mgr)

    assert calls == 1
    (result,) = results(wire)
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
        return asks(stderr=f"still loading ({calls})")

    mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=2)
    await settle(mgr)

    assert calls == 2
    (result,) = results(wire)
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
        return asks()

    mgr.start("req-1", "no_window", "demo", handler)
    await settle(mgr)

    assert calls == 1
    (result,) = results(wire)
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
        return asks() if calls == 1 else JobOutcome(success=True)

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
    await settle(mgr)

    assert parked["pending"] == 1
    assert parked["running"] == 0
    assert parked["quota_running"] == 0
    assert seen[0]["running"] == 1 and seen[0]["quota_running"] == 1
    assert seen[1]["running"] == 1 and seen[1]["quota_running"] == 1
    assert mgr.load()["running"] == 0
    assert hooks == 1
    first = next(r for r in results(wire) if r.payload["request_id"] == "req-1")
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
            return asks()
        raise RuntimeError("boom")

    mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=60)
    await settle(mgr)

    (result,) = results(wire)
    assert result.payload["failure_reason"] == FailureReason.OS_ERROR
    assert result.payload["attempts"] == 2
    assert result.payload["retries_exhausted"] is False


@pytest.mark.asyncio
async def test_a_stop_while_parked_cancels_without_a_result(wire: FakeWire) -> None:
    mgr = JobManager("agent-1", wire.send)

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return asks(after=30)

    mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=60)
    await asyncio.sleep(0.05)
    assert mgr.is_running("req-1")
    await mgr.shutdown_all(CancelReason.SHUTDOWN)

    assert results(wire) == []
    cancelled = [e for e in wire.sent if e.type is MessageType.COMMAND_CANCELLED]
    assert [e.payload["request_id"] for e in cancelled] == ["req-1"]
    assert mgr.load()["running"] == 0


@pytest.mark.asyncio
async def test_each_park_line_names_its_own_attempt(wire: FakeWire) -> None:
    """d2: a reader of the progress stream can tell the first park from the
    second, not just that something is still loading."""
    mgr = JobManager("agent-1", wire.send)
    calls = 0

    async def handler(progress: ProgressCallback) -> JobOutcome:
        nonlocal calls
        calls += 1
        return asks() if calls < 3 else JobOutcome(success=True)

    mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=60)
    await settle(mgr)

    parks = [
        e.payload["message"]
        for e in wire.sent
        if e.type is MessageType.COMMAND_PROGRESS
    ]
    assert len(parks) == 2
    assert re.search(r"\b1\b", parks[0]) and not re.search(r"\b2\b", parks[0])
    assert re.search(r"\b2\b", parks[1]) and not re.search(r"\b1\b", parks[1])


@pytest.mark.asyncio
async def test_a_park_line_says_how_long_and_why(
    wire: FakeWire, clock: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator watching progress sees the wait and the handler's own cause."""

    async def park(delay: float) -> None:
        clock[0] += delay

    sleep_on(monkeypatch, park)
    mgr = JobManager("agent-1", wire.send)
    calls = 0

    async def handler(progress: ProgressCallback) -> JobOutcome:
        nonlocal calls
        calls += 1
        return (
            asks("volume still mounting", after=7)
            if calls == 1
            else JobOutcome(success=True)
        )

    mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=60)
    await settle(mgr)

    parks = [
        e.payload["message"]
        for e in wire.sent
        if e.type is MessageType.COMMAND_PROGRESS
    ]
    assert len(parks) == 1
    assert "7s" in parks[0] and "volume still mounting" in parks[0]


@pytest.mark.asyncio
async def test_a_handler_that_outlives_a_stop_sends_no_result(wire: FakeWire) -> None:
    """A handler that swallows the stop and returns anyway is not a result: the
    cancel already went out, and an ask must not park a closing manager."""
    mgr = JobManager("agent-1", wire.send)
    entered = asyncio.Event()

    async def handler(progress: ProgressCallback) -> JobOutcome:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        return asks(after=30)

    mgr.start("req-1", "wait_for_dep", "demo", handler, retry_window_s=60)
    await entered.wait()
    await asyncio.wait_for(mgr.shutdown_all(CancelReason.SHUTDOWN), timeout=2)

    assert results(wire) == []
    cancelled = [e for e in wire.sent if e.type is MessageType.COMMAND_CANCELLED]
    assert [e.payload["request_id"] for e in cancelled] == ["req-1"]
