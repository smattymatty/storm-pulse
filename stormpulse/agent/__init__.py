"""The Storm Pulse agent: a long-lived WebSocket client over mutual TLS.

Five concurrent tasks per session (heartbeat, metrics, garage refresh,
dispatch, log shipping); exponential reconnect when the session drops.

``Agent`` is a composition root: per-process state plus one public ``run()``.
The actual work lives in free functions in focused submodules:

============================  ==============================================
``bootstrap``                 Assemble registry + log shippers from Config.
``ssl_context``               Construct the mutual-TLS context.
``reconnect``                 Outer connect / run-tasks / backoff loop.
``register``                  Initial register envelope after each connect.
``loops``                     Periodic-loop bodies (heartbeat, metrics, ...).
``dispatch``                  Inbound message dispatch + command execution.
``garage_actions``            Garage-state side effects of command dispatch.
``metadata``                  Build the register-payload command metadata.
``log_batches``               In-flight log-batch tracking.
``long_running``              Resolve long-running commands to handlers.
``signoff_guard``             Sign-off seal predicate + refusal builder.
============================  ==============================================

Submodules read ``Agent``'s state directly - it's all public, except
``_secret``, ``_nonce_store``, ``_ssl_ctx`` (underscore sigil = cryptographic).
"""

from __future__ import annotations

import asyncio
import ssl

from stormpulse.agent import reconnect
from stormpulse.agent.bootstrap import build_agent_dependencies
from stormpulse.agent.log_batches import PendingBatches
from stormpulse.agent.metadata import build_commands_metadata, strip_binary_path
from stormpulse.agent.ssl_context import create_ssl_context
from stormpulse.auth import NonceStore
from stormpulse.commands.jobs import JobManager, LongRunningFactory
from stormpulse.config import Config
from stormpulse.garage.state import GarageState
from stormpulse.logging import (
    LogPositionStore,
    LogShipper,
    PulseLogger,
    StreamingDockerTailer,
)
from stormpulse.signoff import SignoffState

__all__ = [
    "Agent",
    "build_commands_metadata",
    "create_ssl_context",
    "strip_binary_path",
]


class Agent:
    """Async WebSocket agent that connects to the Storm Pulse dashboard."""

    def __init__(
        self,
        config: Config,
        secret: bytes,
        nonce_store: NonceStore,
        ssl_context: ssl.SSLContext,
        shutdown: asyncio.Event,
        *,
        signoff_state: SignoffState,
        log_position_store: LogPositionStore | None = None,
        pulse_logger: PulseLogger | None = None,
    ) -> None:
        self.config = config
        self._secret = secret
        self._nonce_store = nonce_store
        self._ssl_ctx = ssl_context
        self.shutdown = shutdown
        self.pulse_logger = pulse_logger
        self.signoff_state = signoff_state
        deps = build_agent_dependencies(
            config,
            signoff_sealed=self.signoff_state.is_sealed(),
            log_position_store=log_position_store,
        )
        self.registry = deps.registry
        self.long_running_factories: dict[str, LongRunningFactory] = (
            deps.long_running_factories
        )
        self.shippers: dict[str, LogShipper] = deps.shippers
        self.streaming_tailers: list[StreamingDockerTailer] = deps.streaming_tailers
        # ADR GARAGE-000: bootstrap publishes garage_live; runtime reads it.
        self.garage_live: bool = deps.garage_live
        self.garage_state: GarageState | None = (
            GarageState.disabled(deps.garage_disabled_reason)
            if deps.garage_disabled_reason is not None
            else None
        )
        self.pending_batches = PendingBatches()
        # One JobManager per active connection. Recreated on reconnect;
        # jobs do not survive across connections.
        self.job_manager: JobManager | None = None

    async def run(self) -> None:
        """Connect, run tasks, reconnect on failure until shutdown."""
        await reconnect.run_with_backoff(self)
