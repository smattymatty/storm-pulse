"""Tests for stormpulse.commands.jobs.JobManager."""

from __future__ import annotations

import asyncio

import pytest

from stormpulse.commands.jobs import JobManager, JobOutcome, ProgressCallback
from stormpulse.protocol import Envelope, MessageType


class _FakeWire:
    """Captures envelopes the manager would put on the wire.

    Tests assert against ``sent``. Set ``raise_on_send`` to simulate a
    closed connection.
    """

    def __init__(self) -> None:
        self.sent: list[Envelope] = []
        self.raise_on_send: Exception | None = None

    async def send(self, envelope: Envelope) -> None:
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.sent.append(envelope)


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_emits_progress_then_terminal_result() -> None:
    wire = _FakeWire()
    mgr = JobManager("agent-1", wire.send)

    async def handler(progress: ProgressCallback) -> JobOutcome:
        await progress("starting", 0, None, "")
        await progress("running", 1, 3, "step 1")
        await progress("running", 3, 3, "done")
        return JobOutcome(success=True, exit_code=0, stdout="ok")

    mgr.start("req-1", "test_cmd", "test", handler)
    # Drain the manager's task.
    await asyncio.gather(*mgr._jobs.values(), return_exceptions=True)

    types = [e.type for e in wire.sent]
    assert types == [
        MessageType.COMMAND_PROGRESS,
        MessageType.COMMAND_PROGRESS,
        MessageType.COMMAND_PROGRESS,
        MessageType.COMMAND_RESULT,
    ]
    first = wire.sent[0].payload
    assert first["stage"] == "starting"
    assert first["request_id"] == "req-1"
    terminal = wire.sent[-1].payload
    assert terminal["success"] is True
    assert terminal["stdout"] == "ok"
    assert mgr.active_count() == 0


# ---------------------------------------------------------------------------
# Handler exception -> failure result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_exception_becomes_failure_result() -> None:
    wire = _FakeWire()
    mgr = JobManager("agent-1", wire.send)

    async def handler(progress: ProgressCallback) -> JobOutcome:
        await progress("starting", 0, None, "")
        raise RuntimeError("boom")

    mgr.start("req-2", "test_cmd", "test", handler)
    await asyncio.gather(*mgr._jobs.values(), return_exceptions=True)

    terminal = wire.sent[-1]
    assert terminal.type == MessageType.COMMAND_RESULT
    assert terminal.payload["success"] is False
    assert terminal.payload["failure_reason"] == "os_error"
    assert "boom" in terminal.payload["stderr"]
    assert mgr.active_count() == 0


# ---------------------------------------------------------------------------
# Cancellation does NOT emit a terminal result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancellation_skips_terminal_result() -> None:
    wire = _FakeWire()
    mgr = JobManager("agent-1", wire.send)
    started = asyncio.Event()

    async def handler(progress: ProgressCallback) -> JobOutcome:
        await progress("starting", 0, None, "")
        started.set()
        await asyncio.sleep(60)  # would block forever in test
        return JobOutcome(success=True)

    mgr.start("req-3", "test_cmd", "test", handler)
    await started.wait()
    await mgr.shutdown_all()

    # We saw the starting progress, but no terminal result.
    types = [e.type for e in wire.sent]
    assert MessageType.COMMAND_PROGRESS in types
    assert MessageType.COMMAND_RESULT not in types
    assert mgr.active_count() == 0


# ---------------------------------------------------------------------------
# Concurrent jobs don't interfere
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_jobs_run_independently() -> None:
    wire = _FakeWire()
    mgr = JobManager("agent-1", wire.send)
    barrier = asyncio.Event()

    async def slow_handler(progress: ProgressCallback) -> JobOutcome:
        await progress("starting", 0, None, "")
        await barrier.wait()
        return JobOutcome(success=True)

    async def fast_handler(progress: ProgressCallback) -> JobOutcome:
        await progress("starting", 0, None, "")
        return JobOutcome(success=True)

    mgr.start("slow", "cmd_slow", "test", slow_handler)
    mgr.start("fast", "cmd_fast", "test", fast_handler)

    # Wait for fast to finish (slow is parked on the barrier)
    await asyncio.wait_for(mgr._jobs["fast"], timeout=1.0)
    assert mgr.is_running("slow") is True
    assert mgr.is_running("fast") is False

    # Verify fast has both progress + result; slow has only progress so far
    fast_msgs = [e for e in wire.sent if e.payload.get("request_id") == "fast"]
    assert any(e.type == MessageType.COMMAND_RESULT for e in fast_msgs)
    slow_msgs = [e for e in wire.sent if e.payload.get("request_id") == "slow"]
    assert all(e.type == MessageType.COMMAND_PROGRESS for e in slow_msgs)

    # Release the barrier and let slow finish
    barrier.set()
    await asyncio.wait_for(mgr._jobs["slow"], timeout=1.0)
    assert mgr.active_count() == 0


# ---------------------------------------------------------------------------
# Duplicate dispatch is rejected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_request_id_raises() -> None:
    wire = _FakeWire()
    mgr = JobManager("agent-1", wire.send)
    barrier = asyncio.Event()

    async def handler(progress: ProgressCallback) -> JobOutcome:
        await barrier.wait()
        return JobOutcome(success=True)

    mgr.start("dup", "cmd", "test", handler)
    with pytest.raises(ValueError, match="already running"):
        mgr.start("dup", "cmd", "test", handler)

    barrier.set()
    await asyncio.wait_for(mgr._jobs["dup"], timeout=1.0)


# ---------------------------------------------------------------------------
# Send failure on progress doesn't crash the job
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_send_failure_does_not_crash_job() -> None:
    wire = _FakeWire()
    wire.raise_on_send = ConnectionError("ws closed")
    mgr = JobManager("agent-1", wire.send)

    completed = asyncio.Event()

    async def handler(progress: ProgressCallback) -> JobOutcome:
        await progress("starting", 0, None, "")  # this send will fail, swallowed
        completed.set()
        return JobOutcome(success=True)

    mgr.start("req-x", "cmd", "test", handler)
    await asyncio.wait_for(completed.wait(), timeout=1.0)
    await asyncio.gather(*mgr._jobs.values(), return_exceptions=True)
    # No envelopes captured (every send raised), but the handler ran to
    # completion — the failure was contained.
    assert wire.sent == []


# ---------------------------------------------------------------------------
# send_now: one-off envelope emission for synthetic failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_now_puts_envelope_on_wire() -> None:
    wire = _FakeWire()
    mgr = JobManager("agent-1", wire.send)

    from stormpulse.protocol import CommandResultPayload, make_command_result

    payload = CommandResultPayload(
        request_id="r-1", command="x", group="g",
        success=False, exit_code=-1, stdout="", stderr="no handler",
        duration_ms=0, failure_reason="os_error",
    )
    await mgr.send_now(make_command_result("agent-1", payload))
    assert len(wire.sent) == 1
    assert wire.sent[0].type == MessageType.COMMAND_RESULT


@pytest.mark.asyncio
async def test_send_now_swallows_send_errors() -> None:
    wire = _FakeWire()
    wire.raise_on_send = ConnectionError("ws closed")
    mgr = JobManager("agent-1", wire.send)

    from stormpulse.protocol import CommandResultPayload, make_command_result

    payload = CommandResultPayload(
        request_id="r-1", command="x", group="g",
        success=False, exit_code=-1, stdout="", stderr="",
        duration_ms=0, failure_reason="os_error",
    )
    # Should not raise
    await mgr.send_now(make_command_result("agent-1", payload))
    assert wire.sent == []
