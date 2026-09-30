"""Function 9: the declared wire contract matches the code that emits it.

Regenerates the artifact from the live dataclasses and command registry and
compares it to the checked-in ``wire-contract.json``; a disagreement fails the
suite, so a rename or a new param updates the contract in the same commit
(CORE-008 decision 2, under CORE-001's extensibility clause). Stdlib only.
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
        return [
            f"{WIRE_CONTRACT_PATH.name} is missing from the repo root; {_REGENERATE}"
        ]

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

    # The command surface a dispatcher pins against. Reported per command and
    # per param so a validator change names the field, not two documents.
    violations.extend(_diff_commands(on_disk.get("commands") or {}, live["commands"]))

    # The digest a consumer reproduces from the file's own bytes. If this is
    # wrong the file is unusable to the far end even when the shape is right,
    # and nothing on this side would look broken.
    # TODO(CORE-008 decision 1, widened 2026-09-18): the artifact grows from
    # integration state to the protocol payloads a consumer reads field-by-field,
    # starting with CommandProgressPayload. digest_covers and the digest move with
    # it; rollout is artifact first, consumer copy second, agent release third.
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


def _diff_commands(on_disk: Any, live: dict[str, Any]) -> list[str]:
    """Per-command, per-param differences in the declared command surface."""
    if not isinstance(on_disk, dict):
        return [f"commands is not an object; {_REGENERATE}"]
    violations: list[str] = []
    for name in sorted(set(on_disk) | set(live)):
        if name not in live:
            violations.append(
                f"commands: declared command {name!r} is no longer registered; {_REGENERATE}"
            )
        elif name not in on_disk:
            violations.append(
                f"commands: command {name!r} is registered but not declared; {_REGENERATE}"
            )
        else:
            declared = (on_disk[name] or {}).get("params") or {}
            violations.extend(
                _diff_params(f"commands.{name}", declared, live[name]["params"])
            )
    return violations


def _diff_params(
    at: str, declared: dict[str, Any], emitted: dict[str, Any]
) -> list[str]:
    """One line per param present on one side only, or validated differently."""
    gone = sorted(set(declared) - set(emitted))
    new = sorted(set(emitted) - set(declared))
    drifted = [
        p for p in sorted(set(declared) & set(emitted)) if declared[p] != emitted[p]
    ]
    return (
        [f"{at}: declared param {p!r} no longer exists; {_REGENERATE}" for p in gone]
        + [
            f"{at}: param {p!r} is accepted but not declared; {_REGENERATE}"
            for p in new
        ]
        + [
            f"{at}.{p}: declared validator {declared[p]!r} != registered {emitted[p]!r}; "
            f"{_REGENERATE}"
            for p in drifted
        ]
    )
