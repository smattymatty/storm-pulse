"""The Integration contract and its registry (CORE-005 decisions 2, 3, 4).

Sibling of ``init/registry.py``: an Integration registers its contract at
import time and the Entry layer iterates the registered set without importing
any Integration by name. Framework layer, so it imports Foundation only - the
capability signatures that would otherwise pull ``commands/`` up are typed
loosely on purpose.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from stormpulse.config import CommandSpec


class StateBlob(Protocol):
    """An Integration's own state object: opaque to the protocol, owns its to_dict()."""

    def to_dict(self) -> dict[str, Any]: ...


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
StateInterval = Callable[[Any], float]


@dataclass(frozen=True, slots=True)
class Integration:
    """A registered Integration contract (CORE-005 decision 2).

    Required core: ``id``, ``parse_config``, ``enabled``. Everything else is an
    opt-in capability declared only when present, so caddy (no discovery, no
    loop) and a future read-only monitor (no commands) are both legal with no
    empty stubs. The ADR also lists ``cli`` and ``init_step``; both are deferred
    here. ``init_step`` already has its own inversion (``init/registry.py``) and
    folding it in would need a Framework sibling import; the ``cli`` seam is
    outside this diff's bootstrap/reconnect/register/loops scope. Adding either
    is a later, additive change to this descriptor.
    """

    id: str
    parse_config: ParseConfig
    enabled: EnabledPredicate
    preconditions: Preconditions | None = None
    specs: BuildSpecs | None = None
    discover: CollectState | None = None
    collect_state: CollectState | None = None
    state_push_interval: StateInterval | None = None


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
