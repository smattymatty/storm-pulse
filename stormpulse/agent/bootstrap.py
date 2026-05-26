"""Assemble the runtime objects an Agent needs from its Config.

The Agent constructor is composition only. Anything that touches the
file system, merges feature command sets, composes feature
long-running handler factories, or can fail with a ``ConfigError``
lives here so startup failures are raised by the CLI boot path, not
buried inside ``Agent.__init__``.
"""

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
from stormpulse.logging import (
    DockerTailer,
    LogPositionStore,
    LogShipper,
    LogTailer,
    StreamingDockerTailer,
)


@dataclass(frozen=True, slots=True)
class AgentDependencies:
    """The agent's per-process runtime, built once at startup.

    ``registry`` is the resolved command set (built-ins + feature
    additions + signoff sealing applied). ``long_running_factories``
    maps a command name to the closure that builds its ``JobHandler``
    given the runtime params; the dispatcher looks each command up
    here when its CommandDef is marked ``long_running``. ``shippers``
    and ``streaming_tailers`` are the log-shipping plumbing — only
    populated when a ``LogPositionStore`` is supplied.
    """

    registry: dict[str, CommandDef]
    long_running_factories: dict[str, LongRunningFactory]
    shippers: dict[str, LogShipper]
    streaming_tailers: list[StreamingDockerTailer]


def build_agent_dependencies(
    config: Config,
    *,
    signoff_sealed: bool,
    log_position_store: LogPositionStore | None,
) -> AgentDependencies:
    """Assemble the registry, factories, and log shippers an Agent will use.

    Raises ``ConfigError`` when the system is configured with Caddy
    integration but the operator's main Caddyfile does not import our
    drop-in. Failing fast here is deliberate: silent success at this
    point would mean fragments written but never served, which surfaces
    as hung customer activations weeks later.
    """
    commands = dict(config.commands)
    long_running: dict[str, LongRunningFactory] = {}
    if config.garage and config.garage.enabled:
        commands.update(build_garage_commands(config.garage))
        long_running.update(garage_long_running_factories(config.garage))
    if config.caddy and config.caddy.enabled:
        import_err = verify_drop_in_imported(
            config.caddy.main_caddyfile, config.caddy.drop_in_path,
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
    )
