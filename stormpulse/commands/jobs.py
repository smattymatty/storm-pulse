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
from dataclasses import dataclass, field
from typing import Any

from stormpulse import events
from stormpulse.protocol import (
    CommandProgressPayload,
    CommandResultPayload,
    Envelope,
    make_command_progress,
    make_command_result,
)

logger = logging.getLogger(__name__)


ProgressCallback = Callable[[str, int, int | None, str], Awaitable[None]]
"""(stage, current, total, message) -> awaitable.

Stage is one of ``"starting"``, ``"running"``, ``"finalizing"``. The first
call from a handler must use ``"starting"`` with ``current=0``.
"""


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

    def __init__(self, agent_id: str, send: SendCallback) -> None:
        self._agent_id = agent_id
        self._send = send
        self._jobs: dict[str, asyncio.Task[None]] = {}
        self._sem = asyncio.Semaphore(self.MAX_CONCURRENT_JOBS)
        # Jobs currently holding an execution permit (running the handler/hook),
        # distinct from accepted-but-queued. The gap between this and
        # ``active_count()`` is queue pressure; both ride the metrics push (#3).
        self._running = 0

    def start(
        self,
        request_id: str,
        command: str,
        group: str,
        handler: JobHandler,
        on_success: Callable[[], Awaitable[None]] | None = None,
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
            self._run(request_id, command, group, handler, on_success),
            name=f"job:{command}:{request_id}",
        )
        self._jobs[request_id] = task

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
        return {"pending": self.active_count(), "running": self._running}

    async def shutdown_all(self) -> None:
        """Cancel every in-flight job and wait for them to finish.

        Called when the WebSocket connection closes. Jobs that were running
        will receive ``CancelledError`` and exit without emitting a terminal
        result - the dashboard will see the disconnect and reconcile.
        """
        active = [t for t in self._jobs.values() if not t.done()]
        if not active:
            self._jobs.clear()
            return
        logger.info("Cancelling %d in-flight long-running job(s)", len(active))
        for task in active:
            task.cancel()
        # Wait for cancellations to settle. return_exceptions swallows the
        # CancelledErrors so gather doesn't propagate them.
        await asyncio.gather(*active, return_exceptions=True)
        self._jobs.clear()

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
        # on_success hook (both run in this task's context) to the job.
        events.trigger_var.set("job")
        async with self._sem:
            self._running += 1
            try:
                await self._execute(request_id, command, group, handler, on_success)
            finally:
                self._running -= 1

    async def _execute(
        self,
        request_id: str,
        command: str,
        group: str,
        handler: JobHandler,
        on_success: Callable[[], Awaitable[None]] | None = None,
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
        ) -> None:
            payload = CommandProgressPayload(
                request_id=request_id,
                command=command,
                group=group,
                stage=stage,
                current=current,
                total=total,
                message=message,
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
