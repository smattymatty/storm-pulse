"""The Integration contract and its registry (CORE-005 decisions 2, 3, 4).

Sibling of ``init/registry.py``: an Integration registers its contract at
import time and the Entry layer iterates the registered set without importing
any Integration by name. Framework layer, so it imports Foundation only - the
capability signatures that would otherwise pull ``commands/`` up are typed
loosely on purpose.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from stormpulse.config import CommandSpec
from stormpulse.sdk import Capability, CapabilityStatus
from stormpulse.sdk.investigate import CaseFile, Window


class StateBlob(Protocol):
    """An Integration's own state object: opaque to the protocol, owns its to_dict()."""

    def to_dict(self) -> dict[str, Any]: ...


@runtime_checkable
class MergeableState(StateBlob, Protocol):
    """State supporting the targeted upsert merge; required iff ``read_affected`` is declared."""

    def with_items(self, items: Iterable[Any], /) -> MergeableState: ...


# Capability signatures. The parsed config is integration-owned, so it types as
# Any here and the static type re-forms in each Integration's own module.
ParseConfig = Callable[[dict[str, Any]], Any]
EnabledPredicate = Callable[[Any], bool]
Preconditions = Callable[[Any], str | None]
# One seam, not two. Each CommandSpec carries its own schema and (for a job) its
# handler thunk, so an Integration contributes its whole command surface through
# a single builder. The old split (a CommandDef map plus a parallel
# name->factory map that had to agree but were not 1:1) is gone: there is no
# second map to drift against.
BuildSpecs = Callable[[Any], dict[str, CommandSpec]]
CollectState = Callable[[Any], "StateBlob | None"]
# Post-mutation targeted re-read: given config, the current snapshot (id planning
# only), and the mutation's params, return only the freshly re-read items.
ReadAffected = Callable[[Any, Any, Mapping[str, str]], list[Any]]
# Deploy subjects an Integration contributes (CORE-005 D14). Returns SDK
# SdkDeploySubject values; typed loosely here so Framework does not reach
# sideways into a Feature's shapes.
DeploySubjects = Callable[[Any], tuple[Any, ...]]
# Log-line enrichment: (key_id, name) -> resolved id. Built tick-fresh from the
# integration's current state blob; must accept None (no state yet) and stay honest.
LogEnricher = Callable[[str, str], str]
BuildLogEnricher = Callable[[Any], LogEnricher]
# Optional host-touching readiness probe (P2): given the parsed config, report
# the live status of each declared capability. Distinct from ``preconditions``
# (the boot gate) but single-sourced with it - the baseline "is this live"
# derives from ``preconditions``; this probe only adds per-capability detail.
# Runs on the doctor/readiness/init path, NEVER under ``config check`` (CORE-000
# side-effect-free rule; ADR CORE-007 readiness graph).
ReadinessProbe = Callable[[Any], tuple[CapabilityStatus, ...]]


# One Investigation's runner: given the parsed integration config and the
# resolved Window, produce a CaseFile. Fetching evidence (docker logs, admin
# calls) happens inside; the CLI host owns rendering.
RunInvestigation = Callable[[Any, "Window"], "CaseFile"]


@dataclass(frozen=True, slots=True)
class InvestigationSpec:
    """A declared Investigation: name, operator-facing title, and its runner.

    Surfaced as ``stormpulse <id> investigate <name>`` and listed by the bare
    ``stormpulse investigate``. Names are scoped per-Integration; the receipt
    lives inside the produced CaseFile, next to the checks it earned.
    """

    name: str
    title: str
    run: RunInvestigation


@dataclass(frozen=True, slots=True)
class Integration:
    """A registered Integration contract (CORE-005 decision 2): required core is
    ``id``, ``parse_config``, ``enabled``; every other capability is opt-in, no
    empty stubs. ``cli`` and ``init_step`` stay deferred (init has its own inversion)."""

    id: str
    parse_config: ParseConfig
    enabled: EnabledPredicate
    preconditions: Preconditions | None = None
    specs: BuildSpecs | None = None
    discover: CollectState | None = None
    collect_state: CollectState | None = None
    # Optional collector for the on-demand ``{id}_refresh`` command: a
    # variant that must bypass any internal slow-cadence caches, because
    # an explicit refresh means "the operator just changed something".
    # When absent, refresh uses ``collect_state``.
    collect_state_fresh: CollectState | None = None
    # Optional post-mutation targeted re-read; the generic dispatch hook fires
    # it after a mutating job succeeds and pushes the merged snapshot.
    read_affected: ReadAffected | None = None
    # Optional log enrichers keyed by parser name: "my state can enrich lines of
    # this parser". Parser keys are disjoint across Integrations (fitness-checked).
    log_enrichers: Mapping[str, BuildLogEnricher] | None = None
    # Optional versioned capabilities this Integration provides (P2 readiness
    # graph). A capability token has exactly one provider among built-ins; a
    # duplicate is a boot refusal (mirrors the enricher-parser disjointness rule).
    capabilities: tuple[Capability, ...] | None = None
    # Optional host-touching readiness probe. When absent, each declared
    # capability's liveness is derived from ``preconditions`` (single-sourced).
    readiness: ReadinessProbe | None = None
    # Optional one-shot diagnostic investigations (`stormpulse <id> investigate`).
    # Read-only by contract: an investigation observes and reports, never mutates.
    investigations: tuple[InvestigationSpec, ...] | None = None
    # Optional deploy subjects this Integration contributes to the core `deploy`
    # investigation (CORE-005 D14, CORE-009 D10). Takes this Integration's own
    # parsed config, so a subject reflects what the operator configured rather
    # than a guess. Data, never a command: no allow rule binds to it, so it is
    # outside `command_specs_digest` and moves no control-plane pin. The node's
    # own [investigate.deploy.<subject>] table overrides whatever it returns.
    deploy_subjects: DeploySubjects | None = None
    # A raw table ``parse_config`` accepts with no host, filesystem or seal, so
    # the wire contract can build this Integration's commands in any process.
    # Required with ``specs`` or ``collect_state`` (Function 5); values are placeholders.
    declared_config: Mapping[str, Any] | None = None


# Kept byte-identical to the pre-single-source ``garage_refresh`` entry so the
# advertised wire manifest is unchanged. The text is integration-agnostic.
_REFRESH_DESCRIPTION = (
    "Internal command - triggers immediate state collection and metrics push"
)


def refresh_spec(integ_id: str) -> CommandSpec:
    """Synthesize the generic ``{id}_refresh`` command for a state-collecting Integration.

    An agent-owned capability, not a per-integration handler: any Integration
    declaring ``collect_state`` gets it on equal terms, dispatched by the one
    generic routine in ``stormpulse.agent.refresh``. ``mode="refresh"`` carries
    no handler.
    """
    name = f"{integ_id}_refresh"
    return CommandSpec(
        group=integ_id,
        command=[name],
        timeout=30,
        mode="refresh",
        description=_REFRESH_DESCRIPTION,
    )


def integration_command_specs(
    integ: Integration, parsed: Any
) -> dict[str, CommandSpec]:
    """Every command ``integ`` contributes for ``parsed``: its ``specs`` plus the
    synthesized refresh. The one owner of that rule; bootstrap registers the
    result and the wire contract declares it, so neither can drift from the other."""
    specs = dict(integ.specs(parsed)) if integ.specs is not None else {}
    if integ.collect_state is not None:
        specs[f"{integ.id}_refresh"] = refresh_spec(integ.id)
    return specs


_integrations: list[Integration] = []


def register_integration(integration: Integration) -> None:
    """Register an Integration contract. Called at integration-module import time.

    Idempotent by id: re-registering an id already present is a no-op, the
    sibling of ``register_init_step``'s double-import guard.
    """
    if any(existing.id == integration.id for existing in _integrations):
        return
    _integrations.append(integration)


def registered_integrations() -> list[Integration]:
    """Return the registered Integration contracts, in registration order."""
    return list(_integrations)
