"""Function 9: the declared wire shape matches the classes that emit it.

Regenerates the contract from the live dataclasses and compares it to the
checked-in ``wire-contract.json``. They disagree, the suite fails.

Renaming an emitted field is still allowed and always was. What changes is that
the rename must update the declared artifact in the same commit, where a
reviewer sees it as a diff to a contract rather than a diff to a struct. The
asymmetry this closes: the inbound half of the boundary has had a golden fixture
since the admin-API move, and the outbound half had nothing, so a rename passed
mypy, passed every test here, and was breaking for every consumer.

Mechanizes CORE-008 decision 2, authorized by CORE-001's extensibility clause.
Standard library only, so CORE-001 Function 4's three-package runtime allowlist
is untouched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from stormpulse.agent.wire_contract import (
    DIGEST_COVERS,
    build_wire_contract,
    render_wire_contract,
)
from stormpulse.sdk.declaration import canonical_digest

WIRE_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "wire-contract.json"

_REGENERATE = "regenerate with `make wire-contract` and commit the result"


def check_wire_contract() -> list[str]:
    """Return violation strings; empty list means clean."""
    violations: list[str] = []

    if not WIRE_CONTRACT_PATH.is_file():
        return [f"{WIRE_CONTRACT_PATH.name} is missing from the repo root; {_REGENERATE}"]

    on_disk_text = WIRE_CONTRACT_PATH.read_text(encoding="utf-8")
    live = build_wire_contract()

    try:
        on_disk = json.loads(on_disk_text)
    except json.JSONDecodeError as exc:
        return [f"{WIRE_CONTRACT_PATH.name} is not valid JSON ({exc}); {_REGENERATE}"]

    # The shape itself. Reported per class so a rename names the class and the
    # field rather than dumping two documents at the reviewer.
    live_classes = _classes(live)
    disk_classes = _classes(on_disk)
    for integration in sorted(set(live_classes) | set(disk_classes)):
        violations.extend(
            _diff_integration(
                integration,
                disk_classes.get(integration, {}),
                live_classes.get(integration, {}),
            )
        )

    # The digest a consumer reproduces from the file's own bytes. If this is
    # wrong the file is unusable to the far end even when the shape is right,
    # and nothing on this side would look broken.
    covers = on_disk.get("digest_covers")
    if covers != DIGEST_COVERS:
        violations.append(
            f"digest_covers is {covers!r}, expected {DIGEST_COVERS!r}; {_REGENERATE}"
        )
    elif canonical_digest(on_disk.get(covers)) != on_disk.get("digest"):
        violations.append(
            f"{WIRE_CONTRACT_PATH.name} carries a digest that does not match its own "
            f"{covers!r} block, so a consumer holding only this file cannot reproduce "
            f"it; {_REGENERATE}"
        )

    if on_disk.get("digest") != live["digest"]:
        violations.append(
            f"declared digest {on_disk.get('digest')} != live digest {live['digest']}; "
            f"{_REGENERATE}"
        )

    # Byte-level last: a whitespace-only difference is real (it churns the file
    # on the next regeneration) but it is the least interesting thing here, so
    # it reports after the substantive findings and only when they are silent.
    if not violations and on_disk_text != render_wire_contract():
        violations.append(
            f"{WIRE_CONTRACT_PATH.name} agrees in content but not in bytes; {_REGENERATE}"
        )

    return violations


def _classes(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``{integration id: {class name: {field: nesting}}}`` from a contract doc."""
    integrations = contract.get(DIGEST_COVERS) or {}
    if not isinstance(integrations, dict):
        return {}
    return {
        name: (block or {}).get("classes") or {}
        for name, block in integrations.items()
        if isinstance(block, dict)
    }


def _diff_integration(
    integration: str, on_disk: dict[str, Any], live: dict[str, Any]
) -> list[str]:
    """Per-class, per-field differences, phrased as what the author must do."""
    violations: list[str] = []
    for cls in sorted(set(on_disk) | set(live)):
        if cls not in live:
            violations.append(
                f"{integration}: declared class {cls!r} no longer exists; {_REGENERATE}"
            )
            continue
        if cls not in on_disk:
            violations.append(
                f"{integration}: class {cls!r} is emitted but not declared; {_REGENERATE}"
            )
            continue
        declared_fields, live_fields = on_disk[cls] or {}, live[cls]
        for field in sorted(set(declared_fields) - set(live_fields)):
            violations.append(
                f"{integration}.{cls}: declared field {field!r} is no longer emitted "
                f"(renamed or removed); {_REGENERATE}"
            )
        for field in sorted(set(live_fields) - set(declared_fields)):
            violations.append(
                f"{integration}.{cls}: emits undeclared field {field!r}; {_REGENERATE}"
            )
        for field in sorted(set(declared_fields) & set(live_fields)):
            if declared_fields[field] != live_fields[field]:
                violations.append(
                    f"{integration}.{cls}.{field}: declared nesting "
                    f"{declared_fields[field]!r} != emitted {live_fields[field]!r}; "
                    f"{_REGENERATE}"
                )
    return violations
