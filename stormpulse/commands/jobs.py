"""Background-task substrate for long-running commands.

Jobs do not survive an agent reconnect: on WebSocket close every in-flight
task is cancelled, no terminal ``command.result`` is emitted, and the
dashboard infers failure from the disconnect. Handler exceptions other
than ``CancelledError`` become a failure result with
``failure_reason="os_error"``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from stormpulse import events
from stormpulse.protocol import (
    CommandProgressPayload,
    CommandResultPayload,
    Envelope,
    TransferStats,
    make_command_progress,
    make_command_result,
)

logger = logging.getLogger(__name__)


class ProgressCallback(Protocol):
    """(stage, current, total, message, *, transfer=None) -> awaitable.

    Stage is one of ``"starting"``, ``"running"``, ``"finalizing"``. The first
    call from a handler must use ``"starting"`` with ``current=0``.

    ``transfer`` is keyword-only and defaults to None, so every existing
    handler keeps calling this with four positional arguments and is
    unaffected. Only a job that actually moves bytes passes it. This is a
    Protocol rather than the ``Callable`` alias it used to be for exactly
    that reason: ``Callable[...]`` cannot express a keyword-only parameter.
    """

    async def __call__(
        self,
        stage: str,
        current: int,
        total: int | None,
        message: str,
        *,
        transfer: TransferStats | None = None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class JobOutcome:
    """Result body returned by a long-running handler.

    The job manager wraps this in a ``CommandResultPayload``, supplying
    ``request_id``, ``command``, ``group``, and ``duration_ms`` itself.
    Handlers stay focused on what they actually computed.

    ``extras`` is for command-specific summary fields that ride at the top
    level of the wire payload - e.g. ``garage_bucket_clear`` reports
    ``deleted_count``, ``failed_count``, ``errors``, ``error``. Keys must
    not collide with standard ``CommandResultPayload`` field names; the
    job manager merges them via ``make_command_result(extras=...)``.
    """

    success: bool
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    failure_reason: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


JobHandler = Callable[[ProgressCallback], Awaitable[JobOutcome]]

LongRunningFactory = Callable[[dict[str, str]], "JobHandler | None"]
"""Given the validated runtime params, build the ``JobHandler`` for a
long-running command. Returns ``None`` when the command is registered
but cannot be served on this install (e.g. its feature config is
missing). Each Feature publishes a dict of these factories that the
agent composes at bootstrap.
"""
"""A long-running command body.

Receives a progress callback. Returns a ``JobOutcome``. Should not
construct or send envelopes itself.
"""


SendCallback = Callable[[Envelope], Awaitable[None]]
"""How the manager puts a message on the wire."""


_RESERVED_EVENT_FIELDS = frozenset({
    "ts", "source", "kind", "trigger", "status", "failure_reason",
    "error", "duration_ms", "command", "command_ref", "concurrent",
    "bucket_id", "quota_pending", "quota_running", "quota_rejected",
})
_PARAM_CONTEXT_MAX_CHARS = 100


def _param_context(params: dict[str, str] | None) -> dict[str, str]:
    """A job's small params, as wide-event context.

    A quota event should say which quota it set; the params carry that
    story. Values longer than the cap (a caddy tenants manifest) and
    keys that would collide with a typed event field are skipped, so
    context rides the attrs long tail without bloating the wire.
    """
    if not params:
        return {}
    return {
        key: value
        for key, value in params.items()
        if key not in _RESERVED_EVENT_FIELDS
        and len(str(value)) <= _PARAM_CONTEXT_MAX_CHARS
    }


class JobManager:
    """Owns the asyncio.Task for each in-flight long-running command.

    One instance per active WebSocket connection. Recreated on reconnect.
    """

    # Max jobs whose body (handler + on_success hook) runs at once. Both halves hit
    # Garage's serialized admin API, so an unbounded burst of concurrent jobs
    # saturates it - one of the three amplifiers behind the 2026-06-27 incident.
    # The bound is governed by what that serialized API tolerates, not by anything
    # an operator tunes, so it is hardcoded (same discipline as the topology
    # multiple and the targeted-read cap). Acceptance stays unbounded; only
    # execution is capped (see ``_run``).
    MAX_CONCURRENT_JOBS = 6

    # The one command whose ACCEPTANCE is bounded, not just its execution
    # (estate-map: capacity-ledger-headroom, containment B). The Headroom loop
    # re-dispatches the same set_quota mismatches every metrics tick (~5s) until
    # Garage's observed cap catches up; without an admission ceiling that backlog
    # queues without bound behind the six execution permits. One full wave
    # (MAX_CONCURRENT_JOBS accepted quota jobs) is allowed; the next is shed as
    # agent_overloaded so the website stops spinning and the mismatch is retried
    # on a later tick. Quota-only: no other command's acceptance is touched.
    QUOTA_COMMAND = "garage_bucket_set_quota"

    def __init__(self, agent_id: str, send: SendCallback) -> None:
        self._agent_id = agent_id
        self._send = send
        self._jobs: dict[str, asyncio.Task[None]] = {}
        # request_id -> command, so quota jobs can be counted without parsing
        # task names. Cleaned up wherever ``_jobs`` is (both pop sites + clear).
        self._job_command: dict[str, str] = {}
        self._sem = asyncio.Semaphore(self.MAX_CONCURRENT_JOBS)
        # Jobs currently holding an execution permit (running the handler/hook),
        # distinct from accepted-but-queued. The gap between this and
        # ``active_count()`` is queue pressure; both ride the metrics push (#3).
        self._running = 0
        # Quota-specific pressure for the metrics jobs block. ``_quota_running``
        # mirrors ``_running`` but only for set_quota jobs; ``_quota_rejected``
        # is cumulative (never reset) so a shed wave stays visible after drain.
        self._quota_running = 0
        self._quota_rejected = 0

    def start(
        self,
        request_id: str,
        command: str,
        group: str,
        handler: JobHandler,
        on_success: Callable[[], Awaitable[None]] | None = None,
        params: dict[str, str] | None = None,
    ) -> None:
        """Spawn a background task for ``handler``.

        ``on_success``, if provided, is awaited after the terminal
        ``command.result`` is emitted, but only when the job completed
        with ``outcome.success == True``. Used by the agent to fire
        post-mutation hooks - e.g. refreshing Garage state and pushing
        fresh metrics so a stale state push doesn't overwrite the
        just-completed mutation.

        Raises ValueError if a job with this ``request_id`` is already
        running - duplicate dispatch is a caller bug, not something to
        silently swallow.
        """
        if request_id in self._jobs and not self._jobs[request_id].done():
            raise ValueError(f"Job already running: {request_id}")
        logger.info(
            "Starting job %s (request_id=%s, group=%s)",
            command,
            request_id,
            group,
        )
        task = asyncio.create_task(
            self._run(request_id, command, group, handler, on_success, params),
            name=f"job:{command}:{request_id}",
        )
        self._jobs[request_id] = task
        self._job_command[request_id] = command

    def is_running(self, request_id: str) -> bool:
        """Return True if a task for ``request_id`` exists and isn't done."""
        task = self._jobs.get(request_id)
        return task is not None and not task.done()

    async def send_now(self, envelope: Envelope) -> None:
        """Push an envelope to the wire using this connection's send callback.

        Used by the agent to emit one-off results (e.g. a synthetic failure
        when a long-running command has no registered handler). Logs and
        swallows send failures - the caller is in a non-recoverable path.
        """
        try:
            await self._send(envelope)
        except Exception:
            logger.warning(
                "send_now failed for envelope %s (type=%s)",
                envelope.id,
                envelope.type.value,
                exc_info=True,
            )

    def active_count(self) -> int:
        """Number of currently-running jobs. Used by tests and diagnostics."""
        return sum(1 for t in self._jobs.values() if not t.done())

    def load(self) -> dict[str, int]:
        """Queue-depth snapshot for the metrics push (observability #3).

        ``pending`` is every accepted job not yet done (running + waiting for an
        execution permit); ``running`` is those holding a permit (<=
        ``MAX_CONCURRENT_JOBS``). The gap between them is queue pressure - jobs
        parked on the semaphore. This is where the concurrency cap becomes
        observable: under a burst ``running`` pins at the cap while ``pending``
        climbs past it, instead of the old unbounded stampede.
        """
        return {
            "pending": self.active_count(),
            "running": self._running,
            "quota_pending": self._quota_pending(),
            "quota_running": self._quota_running,
            "quota_rejected": self._quota_rejected,
        }

    def _quota_pending(self) -> int:
        """Accepted, not-yet-done set_quota jobs (running + queued for a permit).

        Gated on ``not task.done()`` (the same done-ness ``active_count`` uses),
        so a quota job cancelled while parked on the semaphore drops out here the
        instant its task is done, even before its stale ``_job_command`` entry is
        reaped - the count never inflates.
        """
        return sum(
            1 for rid, task in self._jobs.items()
            if not task.done() and self._job_command.get(rid) == self.QUOTA_COMMAND
        )

    def quota_admission_open(self) -> bool:
        """Whether another ``garage_bucket_set_quota`` job may be accepted.

        The ceiling is one full execution wave (``MAX_CONCURRENT_JOBS``). Past
        it, quota jobs are shed rather than queued (see ``QUOTA_COMMAND``).
        Execution is already capped by the semaphore; this caps ACCEPTANCE,
        quota-only.
        """
        return self._quota_pending() < self.MAX_CONCURRENT_JOBS

    def should_shed_quota(self, command: str) -> bool:
        """True when ``command`` is a quota job and the ceiling is reached.

        The whole quota-identity + ceiling decision lives here so the dispatch
        path stays ignorant of both; it just asks and, on True, calls
        ``reject_quota_overloaded``.
        """
        return command == self.QUOTA_COMMAND and not self.quota_admission_open()

    async def reject_quota_overloaded(
        self,
        request_id: str,
        command: str,
        group: str,
        params: dict[str, str] | None = None,
    ) -> None:
        """Shed a quota job past the admission ceiling.

        Bumps the cumulative rejected counter, emits a queryable ``job_result``
        event carrying current quota pressure (the story an investigator reads a
        week later), and sends the terminal ``agent_overloaded``
        ``command.result`` so the website's dispatch fails fast instead of
        waiting on a job that was never accepted. Runs no handler and mutates no
        Garage/quota state - the mismatch is left for a later tick to retry.
        """
        self._quota_rejected += 1
        events.emit(
            "job_result",
            source="jobs",
            command=command,
            command_ref=request_id,
            status="rejected",
            failure_reason="agent_overloaded",
            error="quota admission ceiling reached",
            duration_ms=0,
            concurrent=len(self._jobs),
            bucket_id=(params or {}).get("bucket_id", ""),
            quota_pending=self._quota_pending(),
            quota_running=self._quota_running,
            quota_rejected=self._quota_rejected,
            **_param_context(params),
        )
        result = CommandResultPayload(
            request_id=request_id,
            command=command,
            group=group,
            success=False,
            exit_code=-1,
            stdout="",
            stderr="Agent quota admission ceiling reached; retry next tick.",
            duration_ms=0,
            failure_reason="agent_overloaded",
        )
        await self.send_now(make_command_result(self._agent_id, result))

    async def shutdown_all(self) -> None:
        """Cancel every in-flight job and wait for them to finish.

        Called when the WebSocket connection closes. Jobs that were running
        will receive ``CancelledError`` and exit without emitting a terminal
        result - the dashboard will see the disconnect and reconcile.
        """
        active = [t for t in self._jobs.values() if not t.done()]
        if not active:
            self._jobs.clear()
            self._job_command.clear()
            return
        logger.info("Cancelling %d in-flight long-running job(s)", len(active))
        for task in active:
            task.cancel()
        # Wait for cancellations to settle. return_exceptions swallows the
        # CancelledErrors so gather doesn't propagate them.
        await asyncio.gather(*active, return_exceptions=True)
        self._jobs.clear()
        self._job_command.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _run(
        self,
        request_id: str,
        command: str,
        group: str,
        handler: JobHandler,
        on_success: Callable[[], Awaitable[None]] | None = None,
        params: dict[str, str] | None = None,
    ) -> None:
        """Acquire one execution permit, then drive the job to its terminal result.

        The cap is HERE, around execution, never on the acceptance path (start()
        keeps a bare create_task): a burst queues for a permit instead of either
        hammering Garage's serialized admin API or blocking the message loop that
        sends keepalives. The permit covers the WHOLE lifecycle - the handler AND
        the on_success hook - because both hit the admin API (the hook fires the
        targeted re-read). ``async with`` releases the permit on every exit -
        normal, crash, or cancellation - so the pool can never leak to a deadlock.
        """
        # Attribute every admin call made by this job's handler AND its
        # on_success hook (both run in this task's context) to the job,
        # and to THIS command, so one command's story is queryable.
        events.trigger_var.set("job")
        events.command_ref_var.set(request_id)
        async with self._sem:
            self._running += 1
            # Mirror the general permit accounting for quota jobs only. Both
            # increments happen AFTER the semaphore is acquired, so a quota job
            # cancelled while parked never touches these counters; the finally
            # runs on normal exit, crash, and cancel-while-running alike, so
            # neither counter can leak (same guarantee as ``_running``).
            is_quota = command == self.QUOTA_COMMAND
            if is_quota:
                self._quota_running += 1
            try:
                await self._execute(request_id, command, group, handler, on_success, params)
            finally:
                self._running -= 1
                if is_quota:
                    self._quota_running -= 1

    async def _execute(
        self,
        request_id: str,
        command: str,
        group: str,
        handler: JobHandler,
        on_success: Callable[[], Awaitable[None]] | None = None,
        params: dict[str, str] | None = None,
    ) -> None:
        """Drive one job from handler through terminal result and on_success hook.

        Runs holding an execution permit (acquired in ``_run``). Timing and the
        concurrency diagnostic are captured here, after the permit, so
        ``duration_ms`` measures actual work, not time spent queued for a permit.
        """
        start = time.monotonic()
        # Jobs spawned right now (running + queued for a permit). At most
        # MAX_CONCURRENT_JOBS run concurrently; the rest wait. So per-job
        # durations reflect contention only among the running set, and a high
        # count here signals queue pressure (jobs waiting on a permit), not that
        # this many ran at once.
        concurrent = len(self._jobs)
        progress = self._make_progress_callback(request_id, command, group)
        outcome: JobOutcome
        try:
            outcome = await handler(progress)
        except asyncio.CancelledError:
            # Agent disconnect or explicit shutdown. Do NOT emit a terminal
            # result - the dashboard infers failure from the disconnect.
            self._jobs.pop(request_id, None)
            self._job_command.pop(request_id, None)
            raise
        except Exception as exc:
            # Use logger.error with exc_info=False (NOT logger.exception) so
            # the traceback locals - which may include signed shell payloads
            # or other handler-bound secrets - do not land in the journal.
            # The handler's own logging is the right place for diagnostics
            # that need stack context.
            logger.error(
                "Job %r (request_id=%s) crashed: %s",
                command,
                request_id,
                exc,
            )
            logger.debug(
                "Traceback for crashed job %s",
                request_id,
                exc_info=True,
            )
            outcome = JobOutcome(
                success=False,
                exit_code=-1,
                stderr=f"Internal error: {exc}",
                failure_reason="os_error",
            )

        self._jobs.pop(request_id, None)
        self._job_command.pop(request_id, None)

        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            "Job %s (request_id=%s) finished success=%s duration_ms=%d concurrent=%d",
            command,
            request_id,
            outcome.success,
            duration_ms,
            concurrent,
        )
        # The durable record of this job's outcome. The command.result
        # below reaches the dashboard once and evaporates with the relay;
        # this event is what an investigator queries a week later
        # (the evaporated-os_error lesson, 2026-07-03).
        events.emit(
            "job_result",
            source="jobs",
            command=command,
            command_ref=request_id,
            status="success" if outcome.success else "failed",
            failure_reason=outcome.failure_reason,
            error=outcome.stderr if not outcome.success else "",
            duration_ms=duration_ms,
            concurrent=concurrent,
            bucket_id=(params or {}).get("bucket_id", ""),
            **_param_context(params),
        )
        result = CommandResultPayload(
            request_id=request_id,
            command=command,
            group=group,
            success=outcome.success,
            exit_code=outcome.exit_code,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            duration_ms=duration_ms,
            failure_reason=outcome.failure_reason,
        )
        envelope = make_command_result(
            self._agent_id,
            result,
            extras=outcome.extras or None,
        )
        try:
            await self._send(envelope)
        except Exception:
            # ERROR not WARNING: silent loss of a signed command's terminal
            # result means the dashboard never learns the outcome. That's a
            # real failure, not a transient blip.
            logger.error(
                "Failed to send terminal command.result for %s",
                request_id,
                exc_info=True,
            )

        if outcome.success and on_success is not None:
            try:
                await on_success()
            except Exception:
                # ERROR not WARNING: on_success skipping means post-mutation
                # cleanup (fresh metrics push, state refresh) never happens.
                logger.error(
                    "on_success callback failed for %s",
                    request_id,
                    exc_info=True,
                )

    def _make_progress_callback(
        self,
        request_id: str,
        command: str,
        group: str,
    ) -> ProgressCallback:
        """Build the per-job progress callback bound to wire identity."""

        async def progress(
            stage: str,
            current: int,
            total: int | None,
            message: str,
            *,
            transfer: TransferStats | None = None,
        ) -> None:
            # The one place TransferStats is flattened onto the wire. Jobs hand
            # up a cohesive value; the payload keeps the flat optional fields
            # the wire contract declares. TransferStats' field names ARE the
            # wire names, so this spreads rather than restating them four
            # times (test_transfer_stats_fields_match_the_progress_payload
            # keeps the two from drifting). No transfer means the payload's
            # own None defaults stand, which is every non-transfer command.
            payload = CommandProgressPayload(
                request_id=request_id,
                command=command,
                group=group,
                stage=stage,
                current=current,
                total=total,
                message=message,
                **(asdict(transfer) if transfer else {}),
            )
            envelope = make_command_progress(self._agent_id, payload)
            try:
                await self._send(envelope)
            except Exception:
                # Don't crash the job if a single progress send fails - the
                # job will keep working and hit the next progress event or
                # the terminal result, where send-failure is logged again.
                logger.warning(
                    "Failed to send command.progress for %s (stage=%s)",
                    request_id,
                    stage,
                )

        return progress
