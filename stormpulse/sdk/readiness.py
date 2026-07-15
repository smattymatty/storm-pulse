"""SDK readiness data types (the four-state dependency model).

Foundation layer: imports nothing intra-package. These are pure data. The
Framework resolver (``stormpulse/integrations/readiness.py``) computes these
values by probing the host; a wizard receives them read-only on its
``InitContext``, which is why the types live in Foundation and not in Framework.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, IntEnum

# A versioned capability token, e.g. ``garage.admin.v1``. Two or more dotted
# segments plus a trailing ``.vN``.
CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+\.v[0-9]+$")


def is_capability_token(token: str) -> bool:
    """Whether ``token`` is a well-formed versioned capability token."""
    return bool(CAPABILITY_RE.match(token))


class ReadinessState(IntEnum):
    """Ordered readiness. ``IntEnum`` so ``>=`` compares naturally; serialize by
    ``.name`` (see ``sdk`` codec) so the wire carries the label, not the int."""

    AVAILABLE = 0
    CONFIGURED = 1
    ENABLED = 2
    READY = 3


class CapabilityLiveness(Enum):
    """Whether a declared capability is live, unmet, or not yet evaluated."""

    LIVE = "live"
    UNMET = "unmet"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Capability:
    """A capability an integration provides (``provided_by`` = its id) or the host
    provides (``provided_by`` = None)."""

    token: str
    provided_by: str | None = None


@dataclass(frozen=True, slots=True)
class CapabilityStatus:
    """The live status of one capability, with a reason and repair when unmet.
    ``reason``/``repair`` MUST NOT carry a secret."""

    token: str
    liveness: CapabilityLiveness
    reason: str | None = None
    repair: str | None = None


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """An integration's derived readiness: its state, per-capability status, and
    any findings. Produced by the Framework resolver, consumed read-only."""

    integration_id: str
    state: ReadinessState
    capabilities: tuple[CapabilityStatus, ...] = ()
    reason: str | None = None

    @property
    def baseline_live(self) -> bool:
        """Whether the integration reached ``READY`` (its baseline capability is live)."""
        return self.state is ReadinessState.READY


@dataclass(frozen=True, slots=True)
class DependencyRequirement:
    """A ``[[requires.integration]]`` entry: a target id, the state it must reach,
    and optionally a specific capability that must be live at that state."""

    id: str
    state: ReadinessState = ReadinessState.READY
    capability: str | None = None
