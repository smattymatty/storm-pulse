"""SDK mutation kinds, the init plan, and the wizard's read-only context
(CORE-007 decision 5).

Foundation layer: imports nothing intra-package except sibling SDK data. A
mutation is *data describing an intended host change and how to undo it*; the
Framework engine owns the forward/verify/inverse implementation per kind. The
wizard returns this data and never touches the host (I2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import ClassVar

from stormpulse.sdk.readiness import ReadinessReport

# TOML scalar values a claimed section may carry.
TomlScalar = str | int | bool | float


class MutationKind(Enum):
    """The v1 typed mutations (CORE-007 D5). No arbitrary shell, generic file
    writer, or service-manager escape hatch: the kinds are closed."""

    CLAIM_TOML_SECTION = "claim_toml_section"
    INSTALL_FILE = "install_file"
    INSTALL_BINARY = "install_binary"
    CREATE_SYSTEMD_USER_UNIT = "create_systemd_user_unit"
    CADDY_DROP_IN = "caddy_drop_in"
    RESTART_OR_RELOAD = "restart_or_reload"
    VERIFY_PROBE = "verify_probe"


@dataclass(frozen=True, slots=True)
class ClaimTomlSection:
    """Create or replace the integration's own ``[section]`` in ``stormpulse.toml``."""

    KIND: ClassVar[MutationKind] = MutationKind.CLAIM_TOML_SECTION
    section: str
    content: dict[str, TomlScalar]


@dataclass(frozen=True, slots=True)
class InstallFile:
    """Install bytes (pinned by ``content_digest``) at a host-owned base + relpath."""

    KIND: ClassVar[MutationKind] = MutationKind.INSTALL_FILE
    rel_target: str
    content_digest: str
    mode: int = 0o644


@dataclass(frozen=True, slots=True)
class InstallBinary:
    """Install an executable (``install_file`` with the exec bit); digest-pinned."""

    KIND: ClassVar[MutationKind] = MutationKind.INSTALL_BINARY
    rel_target: str
    content_digest: str
    mode: int = 0o555


@dataclass(frozen=True, slots=True)
class CreateSystemdUserUnit:
    """Write a systemd *user* unit and daemon-reload."""

    KIND: ClassVar[MutationKind] = MutationKind.CREATE_SYSTEMD_USER_UNIT
    unit_name: str
    content: str


@dataclass(frozen=True, slots=True)
class CaddyDropIn:
    """Add a Caddy drop-in through the ``caddy.drop_in.v1`` capability provider.

    Dispatched by token; the engine never imports ``caddy/`` to apply this (I13,
    the init/orchestrator inversion)."""

    KIND: ClassVar[MutationKind] = MutationKind.CADDY_DROP_IN
    drop_in_name: str
    content: str


@dataclass(frozen=True, slots=True)
class RestartOrReload:
    """Restart or reload a unit, honoring dependency ordering in ``after``.

    Best-effort: a service manager is not transactional (I6)."""

    KIND: ClassVar[MutationKind] = MutationKind.RESTART_OR_RELOAD
    unit: str
    action: str = "restart"  # "restart" | "reload"
    after: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class VerifyProbe:
    """Run an allowlisted readiness probe for a capability. Read-only: no inverse."""

    KIND: ClassVar[MutationKind] = MutationKind.VERIFY_PROBE
    capability: str


# The closed tagged union. The engine matches on member type.
Mutation = (
    ClaimTomlSection
    | InstallFile
    | InstallBinary
    | CreateSystemdUserUnit
    | CaddyDropIn
    | RestartOrReload
    | VerifyProbe
)


def mutation_kind(mutation: Mutation) -> MutationKind:
    """The ``MutationKind`` of a mutation, read from its ``KIND`` class var."""
    return mutation.KIND


@dataclass(frozen=True, slots=True)
class InitPlan:
    """An ordered list of typed mutations, tagged with the SDK version it was built
    against so the host can refuse a plan newer than it understands (I14)."""

    sdk_api: int
    integration_id: str
    mutations: tuple[Mutation, ...]
    summary: str = ""


@dataclass(frozen=True, slots=True)
class InitContext:
    """Read-only facts the host hands the wizard: the install mode, the config
    path, discovered values, and resolved dependency readiness. The wizard reads
    this and returns data; it never mutates the host through it (I2)."""

    mode: str
    config_path: str
    discovered: dict[str, str] = field(default_factory=dict)
    dependencies: tuple[ReadinessReport, ...] = ()
