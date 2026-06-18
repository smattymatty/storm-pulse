"""Generic per-Integration runtime state and the CORE-005 wire envelope.

Replaces the agent's old named ``garage_live`` / ``garage_state`` pair with one
``IntegrationRuntime`` per configured Integration, plus the builder that turns
the runtime set into the ``integrations`` payload the spec pins.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from stormpulse.integrations import Integration

# Status values, per core-005-integrations-wire.md.
STATUS_LIVE = "live"
STATUS_DISABLED_ERROR = "disabled_error"
STATUS_DISABLED_CHOICE = "disabled_choice"


@dataclass(slots=True)
class IntegrationRuntime:
    """Per-process runtime state for one configured Integration.

    ``status`` + ``disabled_reason`` drive the wire envelope; ``descriptor``
    reaches the runtime capabilities (discover, collect_state, interval);
    ``state`` holds the latest collected blob, reassigned by the refresh loop
    (the generic heir of the old ``agent.garage_state`` reassignment).
    """

    id: str
    status: str
    disabled_reason: str | None
    config: Any
    descriptor: Integration
    state: Any = None


def build_integrations_payload(
    runtimes: dict[str, IntegrationRuntime],
) -> dict[str, dict[str, Any]]:
    """Build the CORE-005 ``integrations`` envelope from the runtime set.

    Per the spec: a live Integration carries its state blob (``{}`` until the
    first collection lands); a disabled one carries ``state: null``;
    ``disabled_reason`` is non-null iff ``status == disabled_error``.
    """
    out: dict[str, dict[str, Any]] = {}
    for integ_id, rt in runtimes.items():
        if rt.status == STATUS_LIVE:
            state: dict[str, Any] | None = (
                rt.state.to_dict() if rt.state is not None else {}
            )
        else:
            state = None
        out[integ_id] = {
            "status": rt.status,
            "disabled_reason": rt.disabled_reason,
            "state": state,
        }
    return out
