"""The Storm Pulse agent: a long-lived WebSocket client.

The agent connects to the dashboard over mutual TLS, then runs five
concurrent tasks per connection (heartbeat, metrics push, garage state
refresh, inbound message dispatch, and per-log-group shipping). It
reconnects with exponential backoff when the session drops.

The ``Agent`` class is the composition root. The actual work lives in
focused submodules:

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
"""

from __future__ import annotations

import asyncio
import ssl

from websockets.asyncio.client import ClientConnection

from stormpulse.agent import dispatch, garage_actions, loops, reconnect
from stormpulse.agent.bootstrap import build_agent_dependencies
from stormpulse.agent.log_batches import PendingBatches
from stormpulse.agent.metadata import build_commands_metadata, strip_binary_path
from stormpulse.agent.ssl_context import create_ssl_context
from stormpulse.auth import NonceStore
from stormpulse.commands.jobs import JobManager, LongRunningFactory
from stormpulse.config import CommandDef, Config
from stormpulse.garage.state import GarageState
from stormpulse.logging import (
    LogPositionStore,
    LogShipper,
    PulseLogger,
    StreamingDockerTailer,
)
from stormpulse.protocol import (
    CommandRequestPayload,
    CommandResultPayload,
    Envelope,
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
        log_position_store: LogPositionStore | None = None,
        pulse_logger: PulseLogger | None = None,
        signoff_state: SignoffState | None = None,
    ) -> None:
        self._config = config
        self._secret = secret
        self._nonce_store = nonce_store
        self._ssl_ctx = ssl_context
        self._shutdown = shutdown
        self._pulse_logger = pulse_logger
        # SignoffState gates dashboard verify-block dispatch (ADR
        # CORE-004). Tests construct Agent without a state object;
        # default to an unsealed sentinel so the existing test surface
        # stays unchanged.
        self._signoff_state = signoff_state or SignoffState(
            config.storage.db_path.parent,
        )
        deps = build_agent_dependencies(
            config,
            signoff_sealed=self._signoff_state.is_sealed(),
            log_position_store=log_position_store,
        )
        self._registry = deps.registry
        self._long_running_factories: dict[str, LongRunningFactory] = (
            deps.long_running_factories
        )
        self._shippers: dict[str, LogShipper] = deps.shippers
        self._streaming_tailers: list[StreamingDockerTailer] = deps.streaming_tailers
        # ADR GARAGE-000: a precondition failure at bootstrap skips
        # garage command registration; the reason rides to the
        # dashboard as the initial GarageState until the operator
        # fixes the host and restarts the agent.
        self._garage_disabled_reason: str | None = deps.garage_disabled_reason
        self._garage_state: GarageState | None = (
            GarageState.disabled(self._garage_disabled_reason)
            if self._garage_disabled_reason is not None
            else None
        )
        self._pending_batches = PendingBatches()
        # One JobManager per active connection. Recreated on reconnect;
        # jobs do not survive across connections.
        self._job_manager: JobManager | None = None

    async def run(self) -> None:
        """Connect, run tasks, reconnect on failure until shutdown."""
        await reconnect.run_with_backoff(self)

    # ------------------------------------------------------------------
    # Periodic tasks — bodies live in ``stormpulse.agent.loops``
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self, ws: ClientConnection) -> None:
        await loops.heartbeat_loop(self, ws)

    async def _metrics_loop(self, ws: ClientConnection) -> None:
        await loops.metrics_loop(self, ws)

    async def _garage_loop(self, ws: ClientConnection) -> None:
        await loops.garage_loop(self, ws)

    async def _log_loop(self, ws: ClientConnection, group_name: str) -> None:
        await loops.log_loop(self, ws, group_name)

    # ------------------------------------------------------------------
    # Inbound dispatch — bodies live in ``stormpulse.agent.dispatch``
    # ------------------------------------------------------------------

    async def _receive_loop(self, ws: ClientConnection) -> None:
        await dispatch.receive_loop(self, ws)

    async def _dispatch(self, ws: ClientConnection, raw: str | bytes) -> None:
        await dispatch.dispatch_message(self, ws, raw)

    async def _handle_command_request(
        self,
        ws: ClientConnection,
        envelope: Envelope,
    ) -> None:
        await dispatch.handle_command_request(self, ws, envelope)

    async def _handle_command_sequence(
        self,
        ws: ClientConnection,
        envelope: Envelope,
    ) -> None:
        await dispatch.handle_command_sequence(self, ws, envelope)

    async def _handle_log_batch_ack(self, envelope: Envelope) -> None:
        await dispatch.handle_log_batch_ack(self, envelope)

    async def _handle_garage_refresh(self, request_id: str) -> CommandResultPayload:
        return await garage_actions.handle_garage_refresh(self, request_id)

    async def _dispatch_long_running(
        self,
        request_id: str,
        payload: CommandRequestPayload,
        cmd_def: CommandDef,
    ) -> None:
        await dispatch.dispatch_long_running(self, request_id, payload, cmd_def)
