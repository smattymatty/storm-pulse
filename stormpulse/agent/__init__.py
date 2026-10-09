"""Storm Pulse's composition root and process-lifetime state.

Submodules implement registration, reconnects, periodic loops and dispatch.
The client connects over mutual TLS and retries dropped sessions with backoff.
Only cryptographic state uses private names: secret, nonce store and SSL context.
"""

from __future__ import annotations

import asyncio
import ssl

from stormpulse.agent import reconnect
from stormpulse.agent.bootstrap import build_agent_dependencies
from stormpulse.agent.cert_renew import cert_renew_loop
from stormpulse.agent.identity import boot_id, started_at
from stormpulse.agent.integrations_runtime import IntegrationRuntime
from stormpulse.agent.log_batches import PendingBatches
from stormpulse.agent.metadata import build_commands_metadata, strip_binary_path
from stormpulse.agent.ssl_context import create_ssl_context
from stormpulse.auth import NonceStore
from stormpulse.commands.jobs import JobManager
from stormpulse.config import Config
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
        # Read once per process: every register repeats the same pair (CORE-011 d7).
        self.boot_id: str = boot_id()
        self.started_at: str = started_at()
        deps = build_agent_dependencies(
            config,
            signoff_sealed=self.signoff_state.is_sealed(),
            log_position_store=log_position_store,
        )
        self.registry = deps.registry
        self.shippers: dict[str, LogShipper] = deps.shippers
        self.streaming_tailers: list[StreamingDockerTailer] = deps.streaming_tailers
        # CORE-005: one runtime per configured Integration, keyed by id. The
        # generic heir of the old garage_live/garage_state pair.
        self.integrations: dict[str, IntegrationRuntime] = deps.integrations
        self.pending_batches = PendingBatches()
        # One JobManager per active connection. Recreated on reconnect;
        # jobs do not survive across connections.
        self.job_manager: JobManager | None = None

    async def run(self) -> None:
        """Connect, run tasks, reconnect on failure until shutdown.

        Cert renewal runs beside the reconnect loop, never inside a session,
        so a flap neither repeats nor cancels it (CORE-010 decision 1).
        """
        async with asyncio.TaskGroup() as tg:
            tg.create_task(cert_renew_loop(self))
            await reconnect.run_with_backoff(self)
