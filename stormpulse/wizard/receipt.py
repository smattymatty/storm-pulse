"""The mutation receipt (P2, CORE-007). Canonical-JSON, local only.

Framework layer. A receipt records what a plan applied and how it ended
(``committed`` / ``rolled_back`` / ``partial_rollback``); it is not a load or
command grant. Follows P1's canonical-JSON discipline (sorted keys, compact
separators, one trailing newline) without importing the P1 codec.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from stormpulse.sdk import MutationKind

STATUS_COMMITTED = "committed"
STATUS_ROLLED_BACK = "rolled_back"
STATUS_PARTIAL_ROLLBACK = "partial_rollback"


@dataclass(frozen=True, slots=True)
class AppliedMutation:
    """One step's outcome for the receipt."""

    kind: str
    target: str
    pre_image_digest: str | None = None
    verified: bool = False
    compensated: bool | None = None


@dataclass(frozen=True, slots=True)
class MutationReceipt:
    """The record of one plan application."""

    agent_id: str
    integration_id: str
    sdk_api: int
    plan_summary: str
    status: str
    applied: tuple[AppliedMutation, ...] = ()
    failure: str | None = None
    schema_version: int = 1

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["applied"] = [asdict(a) for a in self.applied]
        return data

    def to_canonical_json(self) -> str:
        return (
            json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
            + "\n"
        )


def applied(
    kind: MutationKind,
    target: str,
    *,
    pre_image_digest: str | None = None,
    verified: bool = False,
    compensated: bool | None = None,
) -> AppliedMutation:
    """Build an ``AppliedMutation`` from a mutation kind."""
    return AppliedMutation(
        kind=kind.value,
        target=target,
        pre_image_digest=pre_image_digest,
        verified=verified,
        compensated=compensated,
    )
