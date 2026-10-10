"""What JobManager admits and what it sheds: the semaphore that bounds execution,
the quota ceiling that bounds acceptance, the load gauges, and the params a
job_result event carries as context.
"""

from __future__ import annotations

import asyncio

import pytest

from stormpulse.commands.jobs import JobManager, JobOutcome, ProgressCallback
from stormpulse.protocol import MessageType
from tests.helpers import FakeWire

# -- the concurrency cap: the semaphore bounds execution, never acceptance ---


@pytest.mark.asyncio
async def test_execution_concurrency_is_capped(wire: FakeWire) -> None:
    """At most MAX_CONCURRENT_JOBS run their handler at once; the rest queue."""
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
async def test_on_success_hook_holds_its_permit(wire: FakeWire) -> None:
    """The permit covers the on_success hook: a job parked in its hook still blocks a new job."""
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
async def test_crashing_jobs_do_not_leak_permits(wire: FakeWire) -> None:
    """A crash must return its permit (async with), or the pool drains to deadlock."""
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
async def test_load_reports_pending_and_caps_running_at_the_semaphore(
    wire: FakeWire,
) -> None:
    """load() is the #3 queue-depth signal: running pins at the cap, pending climbs."""
    mgr = JobManager("agent-1", wire.send)
    assert mgr.load() == {
        "pending": 0,
        "running": 0,
        "quota_pending": 0,
        "quota_running": 0,
        "quota_rejected": 0,
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
    assert load["pending"] == n  # all accepted, none done
    assert load["pending"] - load["running"] == 2  # parked on the semaphore
    # These were non-quota jobs, so the quota-specific gauges stay at zero.
    assert load["quota_pending"] == 0
    assert load["quota_running"] == 0

    barrier.set()
    await asyncio.gather(*mgr._jobs.values(), return_exceptions=True)
    assert mgr.load() == {  # permits + tasks released
        "pending": 0,
        "running": 0,
        "quota_pending": 0,
        "quota_running": 0,
        "quota_rejected": 0,
    }


# -- the quota admission ceiling (containment B) -----------------------------


QUOTA = JobManager.QUOTA_COMMAND


async def _parked(progress: ProgressCallback) -> JobOutcome:
    # A handler that holds its permit until the test cancels it, so accepted
    # jobs stay "in flight" while admission is probed.
    await asyncio.sleep(60)
    return JobOutcome(success=True)


@pytest.mark.asyncio
async def test_quota_admission_open_until_the_ceiling(wire: FakeWire) -> None:
    """Six set_quota jobs are accepted; the seventh closes admission. A
    non-quota command is never shed by the quota ceiling."""
    mgr = JobManager("agent-1", wire.send)

    for i in range(JobManager.MAX_CONCURRENT_JOBS):
        assert mgr.quota_admission_open() is True  # room before this accept
        mgr.start(f"q-{i}", QUOTA, "buckets", _parked)

    assert mgr.quota_admission_open() is False  # six accepted -> shut
    assert mgr.should_shed_quota(QUOTA) is True
    assert mgr.should_shed_quota("rclone_migrate") is False  # non-quota unaffected

    await mgr.shutdown_all()
    assert mgr.quota_admission_open() is False  # an ended session stays closed


@pytest.mark.asyncio
async def test_seventh_quota_job_is_shed_and_its_handler_never_runs(
    wire: FakeWire,
) -> None:
    """Drive the EXACT dispatch gate (should_shed_quota ? reject : start): six
    handlers run, the seventh is shed with an agent_overloaded result and its
    handler never executes."""
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
                rid,
                QUOTA,
                "buckets",
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
    assert sorted(ran) == sorted(admitted)  # the shed 7th never ran
    assert "q-6" not in ran

    shed = [e for e in wire.sent if e.payload.get("request_id") == "q-6"]
    assert len(shed) == 1
    assert shed[0].type == MessageType.COMMAND_RESULT
    assert shed[0].payload["success"] is False
    assert shed[0].payload["failure_reason"] == "agent_overloaded"

    barrier.set()
    await mgr.shutdown_all()


@pytest.mark.asyncio
async def test_reject_emits_a_queryable_job_result_event_with_pressure(
    wire: FakeWire,
) -> None:
    """The shed is a durable wide event an investigator queries later: which
    bucket, the requested quota, and the live pressure gauges."""
    from stormpulse import events

    mgr = JobManager("agent-1", wire.send)
    events.buffer().drain("flush-before")  # clear anything from earlier tests

    await mgr.reject_quota_overloaded(
        "q-x",
        QUOTA,
        "buckets",
        params={"bucket_id": "bkt", "max_size": "9000000000"},
    )

    batch = events.buffer().drain("capture")
    rejected = [
        e
        for e in batch
        if e.get("kind") == "job_result" and e.get("status") == "rejected"
    ]
    assert len(rejected) == 1
    ev = rejected[0]
    assert ev["command"] == QUOTA
    assert ev["command_ref"] == "q-x"
    assert ev["failure_reason"] == "agent_overloaded"
    assert ev["bucket_id"] == "bkt"
    assert ev["params"] == {"max_size": "9000000000"}  # the quota rode as context
    assert ev["quota_rejected"] == 1  # cumulative pressure gauge


@pytest.mark.asyncio
async def test_a_job_with_no_context_params_rides_without_the_field(
    wire: FakeWire,
) -> None:
    """Every param a typed field already carries leaves ``params`` empty, and an
    empty one is elided (CORE-011 d6: sparse events), never shipped as ``{}``."""
    from stormpulse import events

    mgr = JobManager("agent-1", wire.send)
    events.buffer().drain("flush-before")

    await mgr.reject_quota_overloaded(
        "q-y", QUOTA, "buckets", params={"bucket_id": "bkt"}
    )

    (ev,) = [
        e for e in events.buffer().drain("capture") if e.get("kind") == "job_result"
    ]
    assert ev["bucket_id"] == "bkt"
    assert "params" not in ev


@pytest.mark.asyncio
async def test_non_quota_jobs_admit_while_quota_is_full(wire: FakeWire) -> None:
    mgr = JobManager("agent-1", wire.send)

    for i in range(JobManager.MAX_CONCURRENT_JOBS):
        mgr.start(f"q-{i}", QUOTA, "buckets", _parked)
    assert mgr.should_shed_quota(QUOTA) is True

    # A non-quota command is admitted normally even with the quota lane full.
    mgr.start("other", "rclone_migrate", "buckets", _parked)
    assert "other" in mgr._jobs

    await mgr.shutdown_all()


@pytest.mark.asyncio
async def test_quota_running_caps_at_six_and_pending_drains(wire: FakeWire) -> None:
    """Even a burst that beats the gate (direct start) still has EXECUTION
    capped at six by the semaphore; quota_pending shows the queued overflow and
    both drain to zero."""
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
    assert load["quota_running"] == JobManager.MAX_CONCURRENT_JOBS  # capped
    assert load["quota_pending"] == n  # overflow queued

    barrier.set()
    await asyncio.gather(*mgr._jobs.values(), return_exceptions=True)
    load = mgr.load()
    assert load["quota_running"] == 0
    assert load["quota_pending"] == 0


@pytest.mark.asyncio
async def test_quota_rejected_is_cumulative_and_survives_drain(wire: FakeWire) -> None:
    mgr = JobManager("agent-1", wire.send)

    await mgr.reject_quota_overloaded(
        "r1", QUOTA, "buckets", params={"bucket_id": "b1"}
    )
    await mgr.reject_quota_overloaded(
        "r2", QUOTA, "buckets", params={"bucket_id": "b2"}
    )

    load = mgr.load()
    assert load["quota_rejected"] == 2  # cumulative
    assert load["quota_pending"] == 0  # nothing queued
    assert load["quota_running"] == 0  # nothing running


@pytest.mark.asyncio
async def test_quota_counters_stay_correct_when_queued_jobs_are_cancelled(
    wire: FakeWire,
) -> None:
    """Two quota jobs park on the semaphore (never acquire a permit); shutdown
    cancels running AND waiting. quota_running never exceeds six and both gauges
    return to zero - the waiting jobs never touched the running counter."""
    mgr = JobManager("agent-1", wire.send)

    n = JobManager.MAX_CONCURRENT_JOBS + 2  # 2 wait on the semaphore
    for i in range(n):
        mgr.start(f"q-{i}", QUOTA, "buckets", _parked)

    for _ in range(1000):
        await asyncio.sleep(0)
        if mgr.load()["quota_running"] >= JobManager.MAX_CONCURRENT_JOBS:
            break
    assert (
        mgr.load()["quota_running"] == JobManager.MAX_CONCURRENT_JOBS
    )  # never exceeds
    assert mgr.load()["quota_pending"] == n

    await mgr.shutdown_all()
    load = mgr.load()
    assert load["quota_running"] == 0
    assert load["quota_pending"] == 0
    assert mgr._job_command == {}  # command map fully reaped


@pytest.mark.asyncio
async def test_expanded_jobs_block_rides_the_metrics_envelope(wire: FakeWire) -> None:
    """The wider load() survives protocol serialization and metrics-envelope
    construction (the wire is what a consumer actually reads)."""
    from dataclasses import replace

    from stormpulse.protocol import Envelope, make_metrics_push
    from tests.helpers import FAKE_METRICS

    mgr = JobManager("agent-1", wire.send)
    await mgr.reject_quota_overloaded(
        "r1", QUOTA, "buckets", params={"bucket_id": "b1"}
    )

    job_load = mgr.load()
    envelope = make_metrics_push("agent-1", replace(FAKE_METRICS, jobs=job_load))
    round_tripped = Envelope.from_json(envelope.to_json())

    assert round_tripped.payload["jobs"] == job_load
    assert round_tripped.payload["jobs"]["quota_rejected"] == 1


# -- job params as event context ---------------------------------------------


class TestParamContext:
    """Job params ride the job_result event as story context: which
    bucket, which quota. Big values and typed-field duplicates stay off
    the wire."""

    def test_small_params_ride_reserved_and_big_skipped(self) -> None:
        from stormpulse.commands.jobs import _param_context

        out = _param_context(
            {
                "bucket_id": "abc",  # a typed field already carries it
                "max_size": "5000000000",  # the quota story
                "command": "collides",  # a typed field already carries it
                "trigger": "job",  # the envelope already carries it
                "tenants": "x" * 5000,  # a manifest; too big for context
            }
        )
        assert out == {"max_size": "5000000000"}

    def test_none_params_is_empty(self) -> None:
        from stormpulse.commands.jobs import _param_context

        assert _param_context(None) == {}
