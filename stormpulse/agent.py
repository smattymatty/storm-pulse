"""Storm Pulse agent loop — async WebSocket client with heartbeat, metrics, and command dispatch."""

from __future__ import annotations

import asyncio
import logging
import random
import ssl
import time
import uuid
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from stormpulse import __version__
from stormpulse.auth import AuthError, NonceStore, verify_envelope
from stormpulse.commands import (
    CommandError,
    ParamValidationError,
    build_registry,
    execute_command,
    get_command,
)
from stormpulse.config import CommandDef, Config, ProjectConfig, TlsConfig
from stormpulse.garage.commands import build_garage_commands
from stormpulse.garage.discover import discover_garage
from stormpulse.garage.state import GarageState, collect_garage_state
from stormpulse.metrics import collect_metrics
from stormpulse.protocol import (
    CommandRequestPayload,
    CommandResultPayload,
    CommandSequencePayload,
    Envelope,
    MessageType,
    ProtocolError,
    make_command_result,
    make_heartbeat,
    make_metrics_push,
    make_register,
)

logger = logging.getLogger(__name__)

# Dashboard acknowledgement types — received but not actionable.
_ACK_TYPES = {
    MessageType.REGISTER_OK,
    MessageType.HEARTBEAT_ACK,
    MessageType.METRICS_ACK,
    MessageType.COMMAND_RESULT_ACK,
    MessageType.ERROR,
}


def _strip_binary_path(arg: str) -> str:
    """Strip absolute directory from a binary path for display.

    '/usr/bin/docker' -> 'docker', '{project_dir}' -> '{project_dir}'
    """
    if arg.startswith("/") and "/" in arg[1:]:
        return arg.rsplit("/", 1)[1]
    return arg


def _build_commands_metadata(
    registry: dict[str, CommandDef],
    config: ProjectConfig,
) -> dict[str, Any]:
    """Build rich command metadata dict for the register payload.

    Params with no static default get their default from the project config
    (e.g. ``docker_service_name`` comes from the TOML ``[project]`` section).
    """
    # Config values that can serve as param defaults
    config_defaults: dict[str, str] = {
        "docker_service_name": config.docker_service_name,
    }

    result: dict[str, Any] = {}
    for name, cmd_def in sorted(registry.items()):
        template = [_strip_binary_path(part) for part in cmd_def.command]

        params: dict[str, Any] = {}
        for pname, pdef in cmd_def.params.items():
            default = pdef.default
            if default is None:
                default = config_defaults.get(pdef.placeholder)
            params[pname] = {
                "default": default,
                "pattern": pdef.pattern,
                "description": pdef.description,
            }

        result[name] = {
            "group": cmd_def.group,
            "description": cmd_def.description,
            "template": template,
            "timeout": cmd_def.timeout,
            "requires_confirmation": cmd_def.requires_confirmation,
            "params": params,
        }
    return result


def create_ssl_context(tls: TlsConfig) -> ssl.SSLContext:
    """Build a mutual TLS context from config paths."""
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=str(tls.ca_cert))
    ctx.load_cert_chain(certfile=str(tls.client_cert), keyfile=str(tls.client_key))
    return ctx


class Agent:
    """Async WebSocket agent that connects to the Storm Pulse dashboard.

    Manages three concurrent tasks per connection: heartbeat, metrics push,
    and inbound message dispatch. Reconnects with exponential backoff.
    """

    def __init__(
        self,
        config: Config,
        secret: bytes,
        nonce_store: NonceStore,
        ssl_context: ssl.SSLContext,
        shutdown: asyncio.Event,
    ) -> None:
        self._config = config
        self._secret = secret
        self._nonce_store = nonce_store
        self._ssl_ctx = ssl_context
        self._shutdown = shutdown
        # Merge garage commands into registry if enabled
        commands = dict(config.commands)
        if config.garage and config.garage.enabled:
            commands.update(build_garage_commands(config.garage))
        self._registry = build_registry(commands, config.agent.disabled_commands)
        self._garage_state: GarageState | None = None

    # ------------------------------------------------------------------
    # Outer reconnect loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Connect to dashboard, run tasks, reconnect on failure."""
        agent_id = self._config.agent.id
        url = self._config.dashboard.url
        delay = self._config.dashboard.reconnect_min_seconds

        while not self._shutdown.is_set():
            try:
                logger.info("Connecting to %s", url)
                async with connect(
                    url,
                    ssl=self._ssl_ctx,
                    open_timeout=10,
                    ping_interval=20,
                    ping_timeout=20,
                    compression=None,
                ) as ws:
                    logger.info("Connected to dashboard")
                    delay = self._config.dashboard.reconnect_min_seconds

                    # Discover garage state for initial register
                    garage_dict = None
                    if self._config.garage and self._config.garage.enabled:
                        self._garage_state = await asyncio.to_thread(
                            discover_garage, self._config.garage,
                        )
                        if self._garage_state:
                            garage_dict = self._garage_state.to_dict()

                    register = make_register(
                        agent_id, __version__, self._config.agent.pulse_token,
                        commands=_build_commands_metadata(
                            self._registry, self._config.project,
                        ),
                        garage=garage_dict,
                    )
                    await ws.send(register.to_json())
                    logger.info("Sent register (v%s)", __version__)

                    async with asyncio.TaskGroup() as tg:
                        tg.create_task(self._heartbeat_loop(ws))
                        tg.create_task(self._metrics_loop(ws))
                        tg.create_task(self._receive_loop(ws))
                        tg.create_task(self._garage_loop(ws))

            except* ConnectionClosed as eg:
                logger.warning("Connection closed: %s", eg.exceptions[0])
            except* OSError as eg:
                logger.warning("Connection error: %s", eg.exceptions[0])
            except* Exception as eg:
                logger.error("Unexpected error: %s", eg.exceptions[0], exc_info=True)

            if self._shutdown.is_set():
                break

            jitter = random.uniform(0, delay * 0.25)
            wait = delay + jitter
            logger.info("Reconnecting in %.1fs", wait)
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=wait)
                break
            except TimeoutError:
                pass

            delay = min(delay * 1.5, self._config.dashboard.reconnect_max_seconds)

        logger.info("Agent shutting down")

    # ------------------------------------------------------------------
    # Periodic loops
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self, ws: ClientConnection) -> None:
        """Send periodic heartbeats until shutdown or disconnect."""
        interval = self._config.dashboard.heartbeat_interval_seconds
        agent_id = self._config.agent.id
        while not self._shutdown.is_set():
            heartbeat = make_heartbeat(agent_id)
            await ws.send(heartbeat.to_json())
            logger.debug("Sent heartbeat %s", heartbeat.id)
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval)
                return
            except TimeoutError:
                pass

    async def _metrics_loop(self, ws: ClientConnection) -> None:
        """Collect and push metrics at configured intervals."""
        interval = self._config.metrics.push_interval_seconds
        agent_id = self._config.agent.id
        while not self._shutdown.is_set():
            try:
                metrics = await asyncio.to_thread(collect_metrics, self._config)
                # Include latest garage state snapshot if available
                garage_dict = self._garage_state.to_dict() if self._garage_state else None
                envelope = make_metrics_push(agent_id, metrics, garage=garage_dict)
                await ws.send(envelope.to_json())
                logger.debug("Sent metrics push %s", envelope.id)
            except ConnectionClosed:
                raise
            except Exception:
                logger.warning("Failed to collect/send metrics", exc_info=True)
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval)
                return
            except TimeoutError:
                pass

    async def _garage_loop(self, ws: ClientConnection) -> None:
        """Refresh Garage state at configured intervals.

        No-op if config.garage is None or disabled. Updates shared
        _garage_state which the metrics loop reads each cycle.
        """
        gc = self._config.garage
        if gc is None or not gc.enabled:
            return
        interval = gc.state_push_interval_seconds
        while not self._shutdown.is_set():
            try:
                state = await asyncio.to_thread(collect_garage_state, gc)
                if state is not None:
                    self._garage_state = state
                    logger.debug("Refreshed garage state")
            except Exception:
                logger.warning("Failed to collect garage state", exc_info=True)
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval)
                return
            except TimeoutError:
                pass

    # ------------------------------------------------------------------
    # Internal commands
    # ------------------------------------------------------------------

    async def _handle_garage_refresh(self, request_id: str) -> CommandResultPayload:
        """Collect fresh Garage state and update shared state.

        Returns a CommandResultPayload. The caller sends a metrics.push
        with the updated state separately.
        """
        gc = self._config.garage
        if gc is None or not gc.enabled:
            return CommandResultPayload(
                request_id=request_id, command="garage_refresh", group="garage",
                success=False, exit_code=-1, stdout="",
                stderr="Garage integration not enabled",
                duration_ms=0, failure_reason="not_configured",
            )
        start = time.monotonic()
        state = await asyncio.to_thread(collect_garage_state, gc)
        duration_ms = int((time.monotonic() - start) * 1000)
        if state is not None:
            self._garage_state = state
            return CommandResultPayload(
                request_id=request_id, command="garage_refresh", group="garage",
                success=True, exit_code=0,
                stdout=f"Refreshed: {len(state.buckets)} buckets",
                stderr="", duration_ms=duration_ms,
            )
        return CommandResultPayload(
            request_id=request_id, command="garage_refresh", group="garage",
            success=False, exit_code=-1, stdout="",
            stderr="Failed to collect garage state",
            duration_ms=duration_ms, failure_reason="collection_failed",
        )

    # ------------------------------------------------------------------
    # Inbound message handling
    # ------------------------------------------------------------------

    async def _receive_loop(self, ws: ClientConnection) -> None:
        """Receive and dispatch inbound messages."""
        while not self._shutdown.is_set():
            message = await ws.recv()
            try:
                await self._dispatch(ws, message)
            except Exception:
                logger.warning("Error dispatching message", exc_info=True)

    async def _dispatch(self, ws: ClientConnection, raw: str | bytes) -> None:
        """Parse envelope, verify auth, execute command(s)."""
        try:
            envelope = Envelope.from_json(raw)
        except ProtocolError as exc:
            logger.warning("Invalid message: %s", exc)
            return

        if envelope.type in _ACK_TYPES:
            logger.debug("Received %s (%s)", envelope.type.value, envelope.id)
            return

        match envelope.type:
            case MessageType.COMMAND_REQUEST:
                await self._handle_command_request(ws, envelope)
            case MessageType.COMMAND_SEQUENCE:
                await self._handle_command_sequence(ws, envelope)
            case _:
                logger.warning("Unexpected message type: %s", envelope.type.value)

    async def _handle_command_request(
        self, ws: ClientConnection, envelope: Envelope,
    ) -> None:
        """Verify and execute a single command request."""
        try:
            payload = verify_envelope(
                envelope, self._secret, self._nonce_store,
                self._config.auth.command_max_age_seconds,
            )
        except AuthError as exc:
            logger.warning("Auth failed for %s: %s", envelope.id, exc)
            return

        if not isinstance(payload, CommandRequestPayload):
            logger.error("Expected CommandRequestPayload, got %s", type(payload).__name__)
            return
        logger.info("Executing command %r (request %s)", payload.command, envelope.id)

        if payload.command == "garage_refresh":
            result = await self._handle_garage_refresh(envelope.id)
        else:
            try:
                result = await asyncio.to_thread(
                    execute_command, payload.command, self._config.project, envelope.id,
                    registry=self._registry,
                    runtime_params=payload.params or None,
                )
            except (CommandError, ParamValidationError) as exc:
                logger.warning("Command error for %s: %s", envelope.id, exc)
                return

        response = make_command_result(self._config.agent.id, result)
        await ws.send(response.to_json())
        logger.info(
            "Sent result for %r: success=%s, %dms",
            result.command, result.success, result.duration_ms,
        )

        # After garage_refresh, push fresh metrics immediately
        if payload.command == "garage_refresh" and result.success:
            try:
                metrics = await asyncio.to_thread(collect_metrics, self._config)
                garage_dict = self._garage_state.to_dict() if self._garage_state else None
                metrics_env = make_metrics_push(
                    self._config.agent.id, metrics, garage=garage_dict,
                )
                await ws.send(metrics_env.to_json())
                logger.info("Sent immediate metrics push after garage_refresh")
            except Exception:
                logger.warning("Failed to send metrics after garage_refresh", exc_info=True)

    async def _handle_command_sequence(
        self, ws: ClientConnection, envelope: Envelope,
    ) -> None:
        """Verify and execute a command sequence, streaming results."""
        try:
            payload = verify_envelope(
                envelope, self._secret, self._nonce_store,
                self._config.auth.command_max_age_seconds,
            )
        except AuthError as exc:
            logger.warning("Auth failed for sequence %s: %s", envelope.id, exc)
            return

        if not isinstance(payload, CommandSequencePayload):
            logger.error("Expected CommandSequencePayload, got %s", type(payload).__name__)
            return
        logger.info(
            "Executing sequence %s: %s", payload.sequence_id, payload.commands,
        )

        try:
            for name in payload.commands:
                get_command(name, registry=self._registry)
        except CommandError as exc:
            logger.warning("Sequence %s has invalid command: %s", payload.sequence_id, exc)
            return

        agent_id = self._config.agent.id
        project = self._config.project
        for name in payload.commands:
            request_id = str(uuid.uuid4())
            result = await asyncio.to_thread(
                execute_command, name, project, request_id, payload.sequence_id,
                registry=self._registry,
            )
            response = make_command_result(agent_id, result)
            await ws.send(response.to_json())
            logger.info(
                "Sequence %s step %r: success=%s, %dms",
                payload.sequence_id, name, result.success, result.duration_ms,
            )
            if payload.stop_on_failure and not result.success:
                logger.warning(
                    "Sequence %s halted at %r", payload.sequence_id, name,
                )
                break
