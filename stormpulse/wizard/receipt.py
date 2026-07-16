"""The mutation receipt (P2, CORE-007). Canonical-JSON, local only.

Framework layer. A receipt records what a plan applied and how it ended
(``committed`` / ``rolled_back`` / ``partial_rollback``); it is not a load or
command grant. Follows P1's canonical-JSON discipline (sorted keys, compact
separators, one trailing newline) without importing the P1 codec.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from stormpulse.sdk import MutationKind
from stormpulse.wizard.toml_edit import atomic_write_bytes

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
    applied_at: str = ""
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


def persist_receipt(state_dir: Path, receipt: MutationReceipt) -> Path:
    """Atomically write a mutation receipt under the state area, content-addressed
    by its canonical JSON (the same discipline as the P1 install receipts). Every
    apply - committed, rolled back, or partially rolled back - leaves this record."""
    data = receipt.to_canonical_json().encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    directory = state_dir / "wizard" / "receipts" / receipt.integration_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest}.json"
    atomic_write_bytes(path, data, 0o600)
    return path


def list_receipts(state_dir: Path) -> list[Path]:
    """All persisted mutation-receipt paths under the state area (for audit/doctor)."""
    root = state_dir / "wizard" / "receipts"
    return sorted(root.rglob("*.json")) if root.is_dir() else []


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
