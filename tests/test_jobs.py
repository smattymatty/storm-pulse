"""Tests for stormpulse.commands.jobs.JobManager."""

from __future__ import annotations

import asyncio

import pytest

from stormpulse.commands.jobs import JobManager, JobOutcome, ProgressCallback
from stormpulse.protocol import Envelope, MessageType, TransferStats


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
# The wire shape of command.progress
#
# These assert what the agent ACTUALLY puts on the socket, rather than what a
# stand-in was told to produce. A consumer can be written against a simulated
# agent, and a simulator can emit a field this agent has no way to send; both
# sides then pass their own tests while nothing crosses the wire. Only a test
# that inspects the emitted envelope closes that gap.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_progress_envelope_carries_the_structured_transfer_fields() -> None:
    wire = _FakeWire()
    mgr = JobManager("agent-1", wire.send)

    async def handler(progress: ProgressCallback) -> JobOutcome:
        await progress(
            "running", 2048, 4096, "1 of 2 objects",
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
async def test_a_non_transfer_job_leaves_the_transfer_fields_absent() -> None:
    """cert_status and friends call progress with four positional args and
    must be entirely unaffected by the transfer fields existing."""
    wire = _FakeWire()
    mgr = JobManager("agent-1", wire.send)

    async def handler(progress: ProgressCallback) -> JobOutcome:
        await progress("starting", 0, 1, "Checking certificate")
        return JobOutcome(success=True, exit_code=0, stdout="ok")

    mgr.start("req-1", "caddy_cert_status", "caddy", handler)
    await asyncio.gather(*mgr._jobs.values(), return_exceptions=True)

    payload = wire.sent[0].payload
    for key in (
        "rate_bytes_per_sec", "eta_seconds", "objects_current", "objects_total",
    ):
        assert payload[key] is None


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
    # completion - the failure was contained.
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
        request_id="r-1",
        command="x",
        group="g",
        success=False,
        exit_code=-1,
        stdout="",
        stderr="no handler",
        duration_ms=0,
        failure_reason="os_error",
    )
    await mgr.send_now(make_command_result("agent-1", payload))
    assert len(wire.sent) == 1
    assert wire.sent[0].type == MessageType.COMMAND_RESULT


# ---------------------------------------------------------------------------
# on_success callback fires only on successful completion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_success_fires_after_terminal_result_when_outcome_succeeds() -> None:
    wire = _FakeWire()
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
async def test_on_success_does_not_fire_on_failure_outcome() -> None:
    wire = _FakeWire()
    mgr = JobManager("agent-1", wire.send)
    callback_calls: list[str] = []

    async def on_success() -> None:
        callback_calls.append("should-not-fire")

    async def handler(progress: ProgressCallback) -> JobOutcome:
        return JobOutcome(success=False, failure_reason="auth_failed")

    mgr.start("req-fail", "cmd", "test", handler, on_success=on_success)
    await asyncio.gather(*mgr._jobs.values(), return_exceptions=True)
    assert callback_calls == []


@pytest.mark.asyncio
async def test_on_success_does_not_fire_on_handler_exception() -> None:
    wire = _FakeWire()
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
async def test_on_success_does_not_fire_on_cancellation() -> None:
    wire = _FakeWire()
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
async def test_on_success_callback_failure_does_not_crash_job() -> None:
    """Job is already past the terminal-result send by the time the callback
    runs. A bug in the callback must not propagate - log and move on."""
    wire = _FakeWire()
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


@pytest.mark.asyncio
async def test_send_now_swallows_send_errors() -> None:
    wire = _FakeWire()
    wire.raise_on_send = ConnectionError("ws closed")
    mgr = JobManager("agent-1", wire.send)

    from stormpulse.protocol import CommandResultPayload, make_command_result

    payload = CommandResultPayload(
        request_id="r-1",
        command="x",
        group="g",
        success=False,
        exit_code=-1,
        stdout="",
        stderr="",
        duration_ms=0,
        failure_reason="os_error",
    )
    # Should not raise
    await mgr.send_now(make_command_result("agent-1", payload))
    assert wire.sent == []


# ---------------------------------------------------------------------------
# Concurrency cap (the Semaphore: bound execution, never acceptance)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execution_concurrency_is_capped() -> None:
    """At most MAX_CONCURRENT_JOBS run their handler at once; the rest queue."""
    wire = _FakeWire()
    mgr = JobManager("agent-1", wire.send)
    gate = asyncio.Event()
    entered = 0
    peak = 0

    async def handler(progress: ProgressCallback) -> JobOutcome:
        nonlocal entered, peak
        entered += 1
        peak = max(peak, entered)
        await gate.wait()
        entered -= 1
        return JobOutcome(success=True)

    total = JobManager.MAX_CONCURRENT_JOBS + 4
    for i in range(total):
        mgr.start(f"req-{i}", "cmd", "g", handler)

    # Acceptance is unbounded: every job is spawned and live...
    assert mgr.active_count() == total
    # ...but only MAX get past the semaphore into the handler.
    for _ in range(1000):
        if entered == JobManager.MAX_CONCURRENT_JOBS:
            break
        await asyncio.sleep(0)
    assert entered == JobManager.MAX_CONCURRENT_JOBS
    # The overflow stays parked: a few more ticks must not let an extra in.
    for _ in range(20):
        await asyncio.sleep(0)
    assert entered == JobManager.MAX_CONCURRENT_JOBS

    gate.set()
    await asyncio.gather(*mgr._jobs.values(), return_exceptions=True)
    assert peak == JobManager.MAX_CONCURRENT_JOBS
    assert mgr.active_count() == 0


@pytest.mark.asyncio
async def test_on_success_hook_holds_its_permit() -> None:
    """The permit covers the on_success hook: a job parked in its hook still blocks a new job."""
    wire = _FakeWire()
    mgr = JobManager("agent-1", wire.send)
    hook_gate = asyncio.Event()
    handler_runs = 0

    async def fast_handler(progress: ProgressCallback) -> JobOutcome:
        nonlocal handler_runs
        handler_runs += 1
        return JobOutcome(success=True)

    async def blocking_hook() -> None:
        await hook_gate.wait()

    # Fill every permit with a job whose handler is done but whose hook is blocked.
    for i in range(JobManager.MAX_CONCURRENT_JOBS):
        mgr.start(f"hold-{i}", "cmd", "g", fast_handler, on_success=blocking_hook)
    for _ in range(1000):
        if handler_runs == JobManager.MAX_CONCURRENT_JOBS:
            break
        await asyncio.sleep(0)
    assert handler_runs == JobManager.MAX_CONCURRENT_JOBS

    # One more job: its handler must NOT run while the hooks hold every permit.
    mgr.start("extra", "cmd", "g", fast_handler)
    for _ in range(50):
        await asyncio.sleep(0)
    assert handler_runs == JobManager.MAX_CONCURRENT_JOBS

    hook_gate.set()
    await asyncio.gather(*mgr._jobs.values(), return_exceptions=True)
    assert handler_runs == JobManager.MAX_CONCURRENT_JOBS + 1


@pytest.mark.asyncio
async def test_crashing_jobs_do_not_leak_permits() -> None:
    """A crash must return its permit (async with), or the pool drains to deadlock."""
    wire = _FakeWire()
    mgr = JobManager("agent-1", wire.send)

    async def crashing(progress: ProgressCallback) -> JobOutcome:
        raise RuntimeError("boom")

    for i in range(JobManager.MAX_CONCURRENT_JOBS * 2):
        mgr.start(f"crash-{i}", "cmd", "g", crashing)
    await asyncio.gather(*mgr._jobs.values(), return_exceptions=True)

    # If any crash leaked a permit, the pool is short and this hangs (test times out).
    ran = False

    async def ok(progress: ProgressCallback) -> JobOutcome:
        nonlocal ran
        ran = True
        return JobOutcome(success=True)

    for i in range(JobManager.MAX_CONCURRENT_JOBS):
        mgr.start(f"ok-{i}", "cmd", "g", ok)
    await asyncio.gather(*mgr._jobs.values(), return_exceptions=True)
    assert ran is True
    assert mgr.active_count() == 0


@pytest.mark.asyncio
async def test_load_reports_pending_and_caps_running_at_the_semaphore() -> None:
    """load() is the #3 queue-depth signal: running pins at the cap, pending climbs."""
    wire = _FakeWire()
    mgr = JobManager("agent-1", wire.send)
    assert mgr.load() == {
        "pending": 0, "running": 0,
        "quota_pending": 0, "quota_running": 0, "quota_rejected": 0,
    }

    barrier = asyncio.Event()

    async def parked(progress: ProgressCallback) -> JobOutcome:
        await barrier.wait()
        return JobOutcome(success=True)

    n = JobManager.MAX_CONCURRENT_JOBS + 2
    for i in range(n):
        mgr.start(f"job-{i}", "cmd", "g", parked)

    # Let the scheduler run so up-to-cap jobs acquire a permit and park.
    for _ in range(20):
        await asyncio.sleep(0)
        if mgr.load()["running"] >= JobManager.MAX_CONCURRENT_JOBS:
            break

    load = mgr.load()
    assert load["running"] == JobManager.MAX_CONCURRENT_JOBS  # capped, not stampeding
    assert load["pending"] == n                               # all accepted, none done
    assert load["pending"] - load["running"] == 2             # parked on the semaphore
    # These were non-quota jobs, so the quota-specific gauges stay at zero.
    assert load["quota_pending"] == 0
    assert load["quota_running"] == 0

    barrier.set()
    await asyncio.gather(*mgr._jobs.values(), return_exceptions=True)
    assert mgr.load() == {                                     # permits + tasks released
        "pending": 0, "running": 0,
        "quota_pending": 0, "quota_running": 0, "quota_rejected": 0,
    }


class TestParamContext:
    """Job params ride the job_result event as story context: which
    bucket, which quota. Big values and typed-field collisions stay off
    the wire."""

    def test_small_params_ride_reserved_and_big_skipped(self) -> None:
        from stormpulse.commands.jobs import _param_context

        out = _param_context({
            "bucket_id": "abc",            # reserved: typed field, explicit
            "max_size": "5000000000",      # the quota story
            "command": "collides",         # reserved: typed field
            "tenants": "x" * 5000,         # a manifest; too big for context
        })
        assert out == {"max_size": "5000000000"}

    def test_none_params_is_empty(self) -> None:
        from stormpulse.commands.jobs import _param_context

        assert _param_context(None) == {}


# ---------------------------------------------------------------------------
# Quota admission ceiling (containment B: bound ACCEPTANCE of set_quota, not
# just execution). A Headroom metrics-tick storm re-dispatches the same
# set_quota mismatches every ~5s; past one execution wave the manager sheds
# them as agent_overloaded rather than growing an unbounded queued backlog.
# ---------------------------------------------------------------------------

QUOTA = JobManager.QUOTA_COMMAND


async def _parked(progress: ProgressCallback) -> JobOutcome:
    # A handler that holds its permit until the test cancels it, so accepted
    # jobs stay "in flight" while admission is probed.
    await asyncio.sleep(60)
    return JobOutcome(success=True)


@pytest.mark.asyncio
async def test_quota_admission_open_until_the_ceiling() -> None:
    """Six set_quota jobs are accepted; the seventh closes admission. A
    non-quota command is never shed by the quota ceiling."""
    wire = _FakeWire()
    mgr = JobManager("agent-1", wire.send)

    for i in range(JobManager.MAX_CONCURRENT_JOBS):
        assert mgr.quota_admission_open() is True   # room before this accept
        mgr.start(f"q-{i}", QUOTA, "buckets", _parked)

    assert mgr.quota_admission_open() is False       # six accepted -> shut
    assert mgr.should_shed_quota(QUOTA) is True
    assert mgr.should_shed_quota("rclone_migrate") is False  # non-quota unaffected

    await mgr.shutdown_all()
    assert mgr.quota_admission_open() is True         # reopens after drain


@pytest.mark.asyncio
async def test_seventh_quota_job_is_shed_and_its_handler_never_runs() -> None:
    """Drive the EXACT dispatch gate (should_shed_quota ? reject : start): six
    handlers run, the seventh is shed with an agent_overloaded result and its
    handler never executes."""
    wire = _FakeWire()
    mgr = JobManager("agent-1", wire.send)
    barrier = asyncio.Event()
    ran: list[str] = []

    def make_handler(rid: str):
        async def handler(progress: ProgressCallback) -> JobOutcome:
            ran.append(rid)
            await barrier.wait()
            return JobOutcome(success=True)
        return handler

    admitted: list[str] = []
    for i in range(JobManager.MAX_CONCURRENT_JOBS + 1):
        rid = f"q-{i}"
        # This is byte-for-byte the branch dispatch_long_running takes.
        if mgr.should_shed_quota(QUOTA):
            await mgr.reject_quota_overloaded(
                rid, QUOTA, "buckets",
                params={"bucket_id": f"bkt-{i}", "max_size": "5000000000"},
            )
        else:
            mgr.start(rid, QUOTA, "buckets", make_handler(rid))
            admitted.append(rid)

    assert len(admitted) == JobManager.MAX_CONCURRENT_JOBS

    for _ in range(1000):
        if len(ran) >= JobManager.MAX_CONCURRENT_JOBS:
            break
        await asyncio.sleep(0)
    assert sorted(ran) == sorted(admitted)   # the shed 7th never ran
    assert "q-6" not in ran

    shed = [e for e in wire.sent if e.payload.get("request_id") == "q-6"]
    assert len(shed) == 1
    assert shed[0].type == MessageType.COMMAND_RESULT
    assert shed[0].payload["success"] is False
    assert shed[0].payload["failure_reason"] == "agent_overloaded"

    barrier.set()
    await mgr.shutdown_all()


@pytest.mark.asyncio
async def test_reject_emits_a_queryable_job_result_event_with_pressure() -> None:
    """The shed is a durable wide event an investigator queries later: which
    bucket, the requested quota, and the live pressure gauges."""
    from stormpulse import events

    wire = _FakeWire()
    mgr = JobManager("agent-1", wire.send)
    events.buffer().drain("flush-before")  # clear anything from earlier tests

    await mgr.reject_quota_overloaded(
        "q-x", QUOTA, "buckets",
        params={"bucket_id": "bkt", "max_size": "9000000000"},
    )

    batch = events.buffer().drain("capture")
    rejected = [
        e for e in batch
        if e.get("kind") == "job_result" and e.get("status") == "rejected"
    ]
    assert len(rejected) == 1
    ev = rejected[0]
    assert ev["command"] == QUOTA
    assert ev["command_ref"] == "q-x"
    assert ev["failure_reason"] == "agent_overloaded"
    assert ev["bucket_id"] == "bkt"
    assert ev["max_size"] == "9000000000"    # the requested quota rode as context
    assert ev["quota_rejected"] == 1          # cumulative pressure gauge


@pytest.mark.asyncio
async def test_non_quota_jobs_admit_while_quota_is_full() -> None:
    wire = _FakeWire()
    mgr = JobManager("agent-1", wire.send)

    for i in range(JobManager.MAX_CONCURRENT_JOBS):
        mgr.start(f"q-{i}", QUOTA, "buckets", _parked)
    assert mgr.should_shed_quota(QUOTA) is True

    # A non-quota command is admitted normally even with the quota lane full.
    mgr.start("other", "rclone_migrate", "buckets", _parked)
    assert "other" in mgr._jobs

    await mgr.shutdown_all()


@pytest.mark.asyncio
async def test_quota_running_caps_at_six_and_pending_drains() -> None:
    """Even a burst that beats the gate (direct start) still has EXECUTION
    capped at six by the semaphore; quota_pending shows the queued overflow and
    both drain to zero."""
    wire = _FakeWire()
    mgr = JobManager("agent-1", wire.send)
    barrier = asyncio.Event()

    async def parked(progress: ProgressCallback) -> JobOutcome:
        await barrier.wait()
        return JobOutcome(success=True)

    n = JobManager.MAX_CONCURRENT_JOBS + 3
    for i in range(n):
        mgr.start(f"q-{i}", QUOTA, "buckets", parked)

    for _ in range(1000):
        await asyncio.sleep(0)
        if mgr.load()["quota_running"] >= JobManager.MAX_CONCURRENT_JOBS:
            break

    load = mgr.load()
    assert load["quota_running"] == JobManager.MAX_CONCURRENT_JOBS   # capped
    assert load["quota_pending"] == n                               # overflow queued

    barrier.set()
    await asyncio.gather(*mgr._jobs.values(), return_exceptions=True)
    load = mgr.load()
    assert load["quota_running"] == 0
    assert load["quota_pending"] == 0


@pytest.mark.asyncio
async def test_quota_rejected_is_cumulative_and_survives_drain() -> None:
    wire = _FakeWire()
    mgr = JobManager("agent-1", wire.send)

    await mgr.reject_quota_overloaded("r1", QUOTA, "buckets", params={"bucket_id": "b1"})
    await mgr.reject_quota_overloaded("r2", QUOTA, "buckets", params={"bucket_id": "b2"})

    load = mgr.load()
    assert load["quota_rejected"] == 2   # cumulative
    assert load["quota_pending"] == 0    # nothing queued
    assert load["quota_running"] == 0    # nothing running


@pytest.mark.asyncio
async def test_quota_counters_stay_correct_when_queued_jobs_are_cancelled() -> None:
    """Two quota jobs park on the semaphore (never acquire a permit); shutdown
    cancels running AND waiting. quota_running never exceeds six and both gauges
    return to zero - the waiting jobs never touched the running counter."""
    wire = _FakeWire()
    mgr = JobManager("agent-1", wire.send)

    n = JobManager.MAX_CONCURRENT_JOBS + 2   # 2 wait on the semaphore
    for i in range(n):
        mgr.start(f"q-{i}", QUOTA, "buckets", _parked)

    for _ in range(1000):
        await asyncio.sleep(0)
        if mgr.load()["quota_running"] >= JobManager.MAX_CONCURRENT_JOBS:
            break
    assert mgr.load()["quota_running"] == JobManager.MAX_CONCURRENT_JOBS  # never exceeds
    assert mgr.load()["quota_pending"] == n

    await mgr.shutdown_all()
    load = mgr.load()
    assert load["quota_running"] == 0
    assert load["quota_pending"] == 0
    assert mgr._job_command == {}            # command map fully reaped


@pytest.mark.asyncio
async def test_expanded_jobs_block_rides_the_metrics_envelope() -> None:
    """The wider load() survives protocol serialization and metrics-envelope
    construction (the wire is what a consumer actually reads)."""
    from stormpulse.protocol import Envelope, make_metrics_push
    from tests.helpers import FAKE_METRICS

    wire = _FakeWire()
    mgr = JobManager("agent-1", wire.send)
    await mgr.reject_quota_overloaded("r1", QUOTA, "buckets", params={"bucket_id": "b1"})

    job_load = mgr.load()
    envelope = make_metrics_push("agent-1", FAKE_METRICS, job_load=job_load)
    round_tripped = Envelope.from_json(envelope.to_json())

    assert round_tripped.payload["jobs"] == job_load
    assert round_tripped.payload["jobs"]["quota_rejected"] == 1
