"""Assemble registry, factories, log shippers from Config. Anything fs-touching or ``ConfigError``-raising lives here, not in ``Agent.__init__``."""

from __future__ import annotations

from dataclasses import dataclass

from stormpulse.caddy import (
    build_caddy_commands,
    verify_drop_in_imported,
)
from stormpulse.caddy import long_running_factories as caddy_long_running_factories
from stormpulse.commands import build_registry
from stormpulse.commands.jobs import LongRunningFactory
from stormpulse.config import CommandDef, Config, ConfigError
from stormpulse.garage import build_garage_commands
from stormpulse.garage import long_running_factories as garage_long_running_factories
from stormpulse.garage.preconditions import (
    run_preconditions as run_garage_preconditions,
)
from stormpulse.logging import (
    DockerTailer,
    LogPositionStore,
    LogShipper,
    LogTailer,
    StreamingDockerTailer,
)


@dataclass(frozen=True, slots=True)
class AgentDependencies:
    """Per-process runtime built once at startup. ``garage_live`` gates runtime; ``garage_disabled_reason`` seeds the disabled sentinel (ADR GARAGE-000)."""

    registry: dict[str, CommandDef]
    long_running_factories: dict[str, LongRunningFactory]
    shippers: dict[str, LogShipper]
    streaming_tailers: list[StreamingDockerTailer]
    garage_live: bool = False
    garage_disabled_reason: str | None = None


def build_agent_dependencies(
    config: Config,
    *,
    signoff_sealed: bool,
    log_position_store: LogPositionStore | None,
) -> AgentDependencies:
    """Assemble the registry, factories, and log shippers an Agent will use. Raises ``ConfigError`` on Caddy drop-in misconfig."""
    commands = dict(config.commands)
    long_running: dict[str, LongRunningFactory] = {}
    garage_live = False
    garage_disabled_reason: str | None = None
    if config.garage and config.garage.enabled:
        # ADR GARAGE-000: preconditions gate registration; failure rides to dashboard.
        garage_disabled_reason = run_garage_preconditions(config.garage)
        if garage_disabled_reason is None:
            garage_live = True
            commands.update(build_garage_commands(config.garage))
            long_running.update(garage_long_running_factories(config.garage))
    if config.caddy and config.caddy.enabled:
        import_err = verify_drop_in_imported(
            config.caddy.main_caddyfile,
            config.caddy.drop_in_path,
        )
        if import_err:
            raise ConfigError(f"Caddy configuration invalid: {import_err}")
        commands.update(build_caddy_commands(config.caddy))
        long_running.update(caddy_long_running_factories(config.caddy))
    registry = build_registry(
        commands,
        config.agent.disabled_commands,
        signoff_sealed=signoff_sealed,
    )

    shippers: dict[str, LogShipper] = {}
    streaming_tailers: list[StreamingDockerTailer] = []
    if log_position_store is not None:
        for group in config.log_groups:
            if not group.enabled:
                continue
            tailer: LogTailer | DockerTailer | StreamingDockerTailer
            if group.source_type == "docker_stream":
                streaming = StreamingDockerTailer(group, log_position_store)
                streaming_tailers.append(streaming)
                tailer = streaming
            elif group.source_type == "docker":
                tailer = DockerTailer(group, log_position_store)
            else:
                tailer = LogTailer(group, log_position_store)
            shippers[group.name] = LogShipper(group, tailer)

    return AgentDependencies(
        registry=registry,
        long_running_factories=long_running,
        shippers=shippers,
        streaming_tailers=streaming_tailers,
        garage_live=garage_live,
        garage_disabled_reason=garage_disabled_reason,
    )
