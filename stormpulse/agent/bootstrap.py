"""Assemble registry, factories, log shippers from Config. Anything fs-touching or ``ConfigError``-raising lives here, not in ``Agent.__init__``."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import stormpulse.agent.integrations_manifest  # noqa: F401  (registers in-tree Integrations)
from stormpulse.agent.integrations_runtime import (
    STATUS_DISABLED_CHOICE,
    STATUS_DISABLED_ERROR,
    STATUS_LIVE,
    IntegrationRuntime,
)
from stormpulse.commands import build_registry
from stormpulse.config import CommandSpec, Config, ConfigError
from stormpulse.integrations import Integration, registered_integrations
from stormpulse.logging import (
    DockerTailer,
    LogPositionStore,
    LogShipper,
    LogTailer,
    StreamingDockerTailer,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AgentDependencies:
    """Per-process runtime built once at startup. ``integrations`` carries one IntegrationRuntime per configured Integration (CORE-005)."""

    registry: dict[str, CommandSpec]
    shippers: dict[str, LogShipper]
    streaming_tailers: list[StreamingDockerTailer]
    integrations: dict[str, IntegrationRuntime]


# Kept byte-identical to the pre-single-source ``garage_refresh`` entry so the
# advertised wire manifest is unchanged. The text is integration-agnostic.
_REFRESH_DESCRIPTION = (
    "Internal command - triggers immediate state collection and metrics push"
)


def _refresh_spec(integ_id: str) -> CommandSpec:
    """Synthesize the generic ``{id}_refresh`` command for a state-collecting Integration.

    "Refresh my state now" is an agent-owned capability, not a per-integration
    handler: any Integration that declares ``collect_state`` gets it on equal
    terms (garage as much as a third party), dispatched by the one generic
    routine in ``stormpulse.agent.refresh``. ``mode="refresh"`` carries no
    handler.
    """
    name = f"{integ_id}_refresh"
    return CommandSpec(
        group=integ_id,
        command=[name],
        timeout=30,
        mode="refresh",
        description=_REFRESH_DESCRIPTION,
    )


def _resolve_integration(
    integ: Integration,
    raw: dict[str, object],
    commands: dict[str, CommandSpec],
) -> IntegrationRuntime:
    """Parse, gate, and (if live) register one Integration's commands.

    The fatal/soft line lives here (CORE-005 decision 5): a config error, a
    disabled flag, a failed precondition, OR a command surface that won't build
    disables this one Integration with a status the wire reports; the core agent
    and every sibling stay up. A spec that trips the load-time guard soft-disables
    its integration identically whether it is in-repo or third-party - garage gets
    no privilege a third party wouldn't.
    """
    try:
        ic = integ.parse_config(raw)
    except ConfigError as exc:
        return IntegrationRuntime(integ.id, STATUS_DISABLED_ERROR, str(exc), None, integ)
    if not integ.enabled(ic):
        return IntegrationRuntime(integ.id, STATUS_DISABLED_CHOICE, None, ic, integ)
    reason = integ.preconditions(ic) if integ.preconditions is not None else None
    if reason is not None:
        return IntegrationRuntime(integ.id, STATUS_DISABLED_ERROR, reason, ic, integ)
    try:
        integ_specs = dict(integ.specs(ic)) if integ.specs is not None else {}
        if integ.collect_state is not None:
            integ_specs[f"{integ.id}_refresh"] = _refresh_spec(integ.id)
    except Exception as exc:  # noqa: BLE001 - any build failure is a soft-disable, never a crash
        return IntegrationRuntime(
            integ.id,
            STATUS_DISABLED_ERROR,
            f"command registration failed: {exc}",
            ic,
            integ,
        )
    commands.update(integ_specs)
    return IntegrationRuntime(integ.id, STATUS_LIVE, None, ic, integ)


def build_agent_dependencies(
    config: Config,
    *,
    signoff_sealed: bool,
    log_position_store: LogPositionStore | None,
) -> AgentDependencies:
    """Assemble the registry, factories, log shippers, and Integration runtimes an Agent will use.

    Loops the registered Integrations instead of hand-wiring each by name. An
    Integration absent from config does not appear; a present one resolves to a
    live / disabled_error / disabled_choice runtime. No Integration failure
    aborts boot (CORE-005 decision 5).
    """
    commands = dict(config.commands)
    integrations: dict[str, IntegrationRuntime] = {}
    for integ in registered_integrations():
        raw = config.integrations.get(integ.id)
        if raw is None:
            continue
        rt = _resolve_integration(integ, raw, commands)
        integrations[integ.id] = rt
        if rt.status == STATUS_DISABLED_ERROR:
            logger.warning(
                "Integration %r disabled (error): %s. The agent and other "
                "integrations stay up; fix and restart to re-enable.",
                rt.id, rt.disabled_reason,
            )
        elif rt.status == STATUS_DISABLED_CHOICE:
            logger.info("Integration %r present but disabled by config.", rt.id)
        else:
            logger.info("Integration %r live.", rt.id)

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
        shippers=shippers,
        streaming_tailers=streaming_tailers,
        integrations=integrations,
    )
