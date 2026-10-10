"""JobManager from start to the last frame: progress, the terminal result, a
cancellation, the hooks around success, and a graceful stop (CORE-011 d9).
"""

from __future__ import annotations

import asyncio

import pytest

from stormpulse.commands.jobs import JobManager, JobOutcome, ProgressCallback
from stormpulse.protocol import (
    Envelope,
    FailureReason,
    MessageType,
    TransferStats,
)
from tests.helpers import FakeWire

# -- the wire shape of command.progress --------------------------------------


@pytest.mark.asyncio
async def test_progress_envelope_carries_the_structured_transfer_fields(
    wire: FakeWire,
) -> None:
    mgr = JobManager("agent-1", wire.send)

    async def handler(progress: ProgressCallback) -> JobOutcome:
        await progress(
            "running",
            2048,
            4096,
            "1 of 2 objects",
            transfer=TransferStats(
                rate_bytes_per_sec=1024,
                objects_current=1,
                objects_total=2,
                eta_seconds=30,
            ),
        )
        return JobOutcome(success=True, exit_code=0, stdout="ok")

    mgr.start("req-1", "rclone_migrate", "buckets", handler)
    await asyncio.gather(*mgr._jobs.values(), return_exceptions=True)

    payload = wire.sent[0].payload
    # Flat on the wire, exactly as the Protocol Specification declares them.
    assert payload["rate_bytes_per_sec"] == 1024
    assert payload["eta_seconds"] == 30
    assert payload["objects_current"] == 1
    assert payload["objects_total"] == 2


@pytest.mark.asyncio
async def test_a_non_transfer_job_leaves_the_transfer_fields_absent(
    wire: FakeWire,
) -> None:
    """cert_status and friends call progress with four positional args and
    must be entirely unaffected by the transfer fields existing."""
    mgr = JobManager("agent-1", wire.send)

    async def handler(progress: ProgressCallback) -> JobOutcome:
        await progress("starting", 0, 1, "Checking certificate")
        return JobOutcome(success=True, exit_code=0, stdout="ok")

    mgr.start("req-1", "caddy_cert_status", "caddy", handler)
    await asyncio.gather(*mgr._jobs.values(), return_exceptions=True)

    payload = wire.sent[0].payload
    for key in (
        "rate_bytes_per_sec",
        "eta_seconds",
        "objects_current",
        "objects_total",
    ):
        assert payload[key] is None


# -- success, failure, cancellation ------------------------------------------


@pytest.mark.asyncio
async def test_job_emits_progress_then_terminal_result(wire: FakeWire) -> None:
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


@pytest.mark.asyncio
async def test_handler_exception_becomes_failure_result(wire: FakeWire) -> None:
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
    assert "RuntimeError" in terminal.payload["stderr"]
    assert "integration doctor" in terminal.payload["stderr"]
    assert mgr.active_count() == 0


@pytest.mark.asyncio
async def test_cancellation_skips_terminal_result(wire: FakeWire) -> None:
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

    # We saw the starting progress, but no terminal result, and no cancelled
    # frame either: no reason means no socket to send one on.
    types = [e.type for e in wire.sent]
    assert MessageType.COMMAND_PROGRESS in types
    assert MessageType.COMMAND_RESULT not in types
    assert MessageType.COMMAND_CANCELLED not in types
    assert mgr.active_count() == 0


@pytest.mark.asyncio
async def test_concurrent_jobs_run_independently(wire: FakeWire) -> None:
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


@pytest.mark.asyncio
async def test_duplicate_request_id_raises(wire: FakeWire) -> None:
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


@pytest.mark.asyncio
async def test_progress_send_failure_does_not_crash_job(wire: FakeWire) -> None:
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
    # completion - the failure was contained.
    assert wire.sent == []


# -- the wire shape of command.result (CORE-011 d9) --------------------------


@pytest.mark.asyncio
async def test_job_figures_ride_under_result_and_a_failure_carries_an_error(
    wire: FakeWire,
) -> None:
    """What the socket sees: extras nested under ``result``, never merged into the
    top level; a failed job's reason and stderr as its ``error``, a success's null."""
    mgr = JobManager("agent-1", wire.send)

    async def cleared(progress: ProgressCallback) -> JobOutcome:
        return JobOutcome(success=True, extras={"deleted_count": 3, "success": False})

    async def stalled(progress: ProgressCallback) -> JobOutcome:
        return JobOutcome(
            success=False,
            exit_code=-1,
            stderr="Clear stalled: 503",
            failure_reason=FailureReason.CLEAR_STALLED,
            extras={"deleted_count": 1},
        )

    mgr.start("ok", "garage_bucket_clear", "buckets", cleared)
    mgr.start("bad", "garage_bucket_clear", "buckets", stalled)
    await asyncio.gather(*mgr._jobs.values(), return_exceptions=True)

    by_id = {e.payload["request_id"]: e.payload for e in wire.sent}
    assert by_id["ok"]["result"] == {"deleted_count": 3, "success": False}
    assert by_id["ok"]["success"] is True and by_id["ok"]["error"] is None
    assert "deleted_count" not in by_id["ok"]
    assert by_id["bad"]["result"] == {"deleted_count": 1}
    assert by_id["bad"]["error"] == {
        "code": "clear_stalled",
        "http_status": None,
        "upstream_code": None,
        "message": "Clear stalled: 503",
    }


@pytest.mark.asyncio
async def test_a_handler_that_parsed_an_upstream_code_keeps_it(wire: FakeWire) -> None:
    """A ``JobOutcome``'s ``upstream_code`` (rclone's exit) or ``error_code`` (an
    adapter's own word) rides as the result's ``error``; the derived one is
    only for a handler that set neither."""
    mgr = JobManager("agent-1", wire.send)

    async def exited(progress: ProgressCallback) -> JobOutcome:
        return JobOutcome(
            success=False,
            exit_code=42,
            stderr="rclone: boom",
            failure_reason=FailureReason.RCLONE_ERROR,
            upstream_code="42",
        )

    mgr.start("x", "rclone_migrate", "rclone", exited)
    await asyncio.gather(*mgr._jobs.values(), return_exceptions=True)
    (sent,) = wire.sent
    assert sent.payload["failure_reason"] == "rclone_error"
    assert sent.payload["error"]["upstream_code"] == "42"


# -- send_now: one-off envelope emission for synthetic failures --------------


@pytest.mark.asyncio
async def test_send_now_puts_envelope_on_wire(wire: FakeWire) -> None:
    mgr = JobManager("agent-1", wire.send)

    from stormpulse.protocol import CommandResultPayload, make

    payload = CommandResultPayload(
        request_id="r-1",
        command="x",
        group="g",
        success=False,
        exit_code=-1,
        stdout="",
        stderr="no handler",
        duration_ms=0,
        failure_reason=FailureReason.OS_ERROR,
    )
    await mgr.send_now(make("agent-1", payload))
    assert len(wire.sent) == 1
    assert wire.sent[0].type == MessageType.COMMAND_RESULT


@pytest.mark.asyncio
async def test_send_now_swallows_send_errors(wire: FakeWire) -> None:
    wire.raise_on_send = ConnectionError("ws closed")
    mgr = JobManager("agent-1", wire.send)

    from stormpulse.protocol import CommandResultPayload, make

    payload = CommandResultPayload(
        request_id="r-1",
        command="x",
        group="g",
        success=False,
        exit_code=-1,
        stdout="",
        stderr="",
        duration_ms=0,
        failure_reason=FailureReason.OS_ERROR,
    )
    # Should not raise
    await mgr.send_now(make("agent-1", payload))
    assert wire.sent == []


# -- on_success fires only on a successful completion ------------------------


@pytest.mark.asyncio
async def test_on_success_fires_after_terminal_result_when_outcome_succeeds(
    wire: FakeWire,
) -> None:
    mgr = JobManager("agent-1", wire.send)
    callback_calls: list[str] = []

    async def on_success() -> None:
        callback_calls.append("fired")

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return JobOutcome(success=True)

    mgr.start("req-ok", "cmd", "test", handler, on_success=on_success)
    await asyncio.gather(*mgr._jobs.values(), return_exceptions=True)
    assert callback_calls == ["fired"]


@pytest.mark.asyncio
async def test_on_success_does_not_fire_on_failure_outcome(wire: FakeWire) -> None:
    mgr = JobManager("agent-1", wire.send)
    callback_calls: list[str] = []

    async def on_success() -> None:
        callback_calls.append("should-not-fire")

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return JobOutcome(success=False, failure_reason=FailureReason.AUTH_FAILED)

    mgr.start("req-fail", "cmd", "test", handler, on_success=on_success)
    await asyncio.gather(*mgr._jobs.values(), return_exceptions=True)
    assert callback_calls == []


@pytest.mark.asyncio
async def test_on_success_does_not_fire_on_handler_exception(wire: FakeWire) -> None:
    mgr = JobManager("agent-1", wire.send)
    callback_calls: list[str] = []

    async def on_success() -> None:
        callback_calls.append("should-not-fire")

    async def handler(progress: ProgressCallback) -> JobOutcome:
        raise RuntimeError("boom")

    mgr.start("req-crash", "cmd", "test", handler, on_success=on_success)
    await asyncio.gather(*mgr._jobs.values(), return_exceptions=True)
    assert callback_calls == []


@pytest.mark.asyncio
async def test_on_success_does_not_fire_on_cancellation(wire: FakeWire) -> None:
    mgr = JobManager("agent-1", wire.send)
    callback_calls: list[str] = []
    started = asyncio.Event()

    async def on_success() -> None:
        callback_calls.append("should-not-fire")

    async def handler(progress: ProgressCallback) -> JobOutcome:
        started.set()
        await asyncio.sleep(60)
        return JobOutcome(success=True)

    mgr.start("req-cancel", "cmd", "test", handler, on_success=on_success)
    await started.wait()
    await mgr.shutdown_all()
    assert callback_calls == []


@pytest.mark.asyncio
async def test_on_success_callback_failure_does_not_crash_job(wire: FakeWire) -> None:
    """Job is already past the terminal-result send by the time the callback
    runs. A bug in the callback must not propagate - log and move on."""
    mgr = JobManager("agent-1", wire.send)

    async def on_success() -> None:
        raise RuntimeError("callback bug")

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return JobOutcome(success=True)

    mgr.start("req-cb-bug", "cmd", "test", handler, on_success=on_success)
    # Should not raise out of gather
    await asyncio.gather(*mgr._jobs.values(), return_exceptions=True)
    # Terminal result was still emitted
    assert any(e.type == MessageType.COMMAND_RESULT for e in wire.sent)


# -- a graceful stop: command.cancelled per in-flight job (CORE-011 d9) ------


@pytest.mark.asyncio
async def test_a_graceful_stop_sends_one_command_cancelled_per_in_flight_job(
    wire: FakeWire,
) -> None:
    """A running job and one parked on the semaphore each get the frame, after
    their cancel settled so nothing of theirs follows it; a job that already
    sent its result gets none. Without a reason (the socket is gone) the
    existing test above holds: nothing is sent."""
    from stormpulse.protocol import CancelReason

    order: list[str] = []

    async def send(envelope: Envelope) -> None:
        order.append(f"sent:{envelope.type.value}")
        await wire.send(envelope)

    mgr = JobManager("agent-1", send)
    mgr._sem = asyncio.Semaphore(1)
    started = asyncio.Event()

    async def blocks(progress: ProgressCallback) -> JobOutcome:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            order.append("cancelled")
            raise
        return JobOutcome(success=True)

    async def quick(progress: ProgressCallback) -> JobOutcome:
        return JobOutcome(success=True)

    mgr.start("req-done", "cmd_quick", "test", quick)
    await asyncio.wait_for(mgr._jobs["req-done"], timeout=1.0)
    mgr.start("req-run", "garage_bucket_clear", "garage", blocks)
    await started.wait()
    mgr.start("req-parked", "rclone_migrate", "buckets", blocks)
    await asyncio.sleep(0)

    await mgr.shutdown_all(CancelReason.SHUTDOWN)

    cancelled = [e for e in wire.sent if e.type is MessageType.COMMAND_CANCELLED]
    assert [e.payload for e in cancelled] == [
        {
            "request_id": "req-run",
            "command": "garage_bucket_clear",
            "reason": "shutdown",
        },
        {"request_id": "req-parked", "command": "rclone_migrate", "reason": "shutdown"},
    ]
    # The parked job never ran, so one "cancelled"; both frames go out after it.
    assert order == [
        "sent:command.result",
        "cancelled",
        "sent:command.cancelled",
        "sent:command.cancelled",
    ]
    assert mgr.active_count() == 0


@pytest.mark.asyncio
async def test_a_graceful_stop_survives_a_socket_that_dies_mid_fan_out(
    wire: FakeWire,
) -> None:
    """A SIGTERM can land on a socket already half-dead. The cancelled frames go
    through the swallowing send, so a raise there never escapes ``shutdown_all``:
    the watcher still reaches ``ws.close()`` instead of systemd's SIGKILL."""
    from stormpulse.protocol import CancelReason

    mgr = JobManager("agent-1", wire.send)
    started = asyncio.Event()

    async def blocks(progress: ProgressCallback) -> JobOutcome:
        started.set()
        await asyncio.sleep(60)
        return JobOutcome(success=True)

    mgr.start("req-run", "garage_bucket_clear", "garage", blocks)
    await started.wait()
    wire.raise_on_send = ConnectionError("socket gone")

    await mgr.shutdown_all(CancelReason.SHUTDOWN)

    assert wire.sent == []
    assert mgr.active_count() == 0
