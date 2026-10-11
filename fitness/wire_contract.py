"""Function 9: the declared wire contract matches the code that emits it.

Regenerates the artifact from the live dataclasses, message catalogue and
command registry and compares it to the checked-in ``wire-contract.json``; a
disagreement fails the suite, so a rename, a retype or a new param updates the
contract in the same commit (CORE-008 d2, CORE-011 d3, CORE-001). Stdlib only.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from stormpulse.agent.wire_contract import (
    CONTRACT_DIGEST_COVERS,
    DIGEST_COVERS,
    ENVELOPE_ENTRY,
    build_wire_contract,
    contract_digest,
    render_wire_contract,
)
from stormpulse.sdk.declaration import canonical_digest

WIRE_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "wire-contract.json"

_REGENERATE = "regenerate with `make wire-contract` and commit the result"


def check_wire_contract() -> list[str]:
    """Return violation strings; empty list means clean."""
    if not WIRE_CONTRACT_PATH.is_file():
        return [
            f"{WIRE_CONTRACT_PATH.name} is missing from the repo root; {_REGENERATE}"
        ]
    on_disk_text = WIRE_CONTRACT_PATH.read_text(encoding="utf-8")
    try:
        on_disk = json.loads(on_disk_text)
    except json.JSONDecodeError as exc:
        return [f"{WIRE_CONTRACT_PATH.name} is not valid JSON ({exc}); {_REGENERATE}"]

    live = build_wire_contract()
    # The shape first, per class, field, command and param, so a rename names
    # the attribute; then the digests a consumer reproduces from the file's bytes.
    violations = [
        *_diff_integrations(on_disk, live),
        *_diff_messages(on_disk.get("messages") or {}, live["messages"]),
        *_unit_violations(live["messages"]),
        *_diff_commands(on_disk.get("commands") or {}, live["commands"]),
        *_diff_vectors(on_disk.get("vectors") or {}, live["vectors"]),
        *_diff_v1_digest(on_disk, live),
        *_diff_digest(
            "contract_digest",
            CONTRACT_DIGEST_COVERS,
            contract_digest(on_disk),
            on_disk,
            live,
        ),
    ]
    # Byte-level last: whitespace churn is real but the least interesting
    # finding, so it reports only when the substantive ones are silent.
    if not violations and on_disk_text != render_wire_contract():
        violations.append(
            f"{WIRE_CONTRACT_PATH.name} agrees in content but not in bytes; {_REGENERATE}"
        )
    return violations


def _diff_integrations(on_disk: dict[str, Any], live: dict[str, Any]) -> list[str]:
    """Per integration, per class: names and nesting alone (CORE-008 decision 5)."""
    live_classes, disk_classes = _classes(live), _classes(on_disk)
    return [
        line
        for integration in sorted(set(live_classes) | set(disk_classes))
        for line in _diff_classes(
            integration,
            disk_classes.get(integration, {}),
            live_classes.get(integration, {}),
            _diff_nesting,
        )
    ]


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


_Drift = Callable[[str, Any, Any], list[str]]


def _diff_classes(
    at: str, on_disk: dict[str, Any], live: dict[str, Any], drift: _Drift
) -> list[str]:
    """Per-class, per-field differences, phrased as what the author must do."""
    violations: list[str] = []
    for cls in sorted(set(on_disk) | set(live)):
        if cls not in live:
            violations.append(
                f"{at}: declared class {cls!r} no longer exists; {_REGENERATE}"
            )
        elif cls not in on_disk:
            violations.append(
                f"{at}: class {cls!r} is emitted but not declared; {_REGENERATE}"
            )
        else:
            violations.extend(
                _diff_fields(f"{at}.{cls}", on_disk[cls] or {}, live[cls], drift)
            )
    return violations


def _diff_fields(
    at: str, declared: dict[str, Any], live: dict[str, Any], drift: _Drift
) -> list[str]:
    """One line per field on one side only; ``drift`` compares a field on both."""
    violations: list[str] = []
    for field in sorted(set(declared) - set(live)):
        violations.append(
            f"{at}: declared field {field!r} is no longer emitted "
            f"(renamed or removed); {_REGENERATE}"
        )
    for field in sorted(set(live) - set(declared)):
        violations.append(f"{at}: emits undeclared field {field!r}; {_REGENERATE}")
    for field in sorted(set(declared) & set(live)):
        violations.extend(drift(f"{at}.{field}", declared[field], live[field]))
    return violations


def _drift_line(at: str, attr: str, declared: Any, emitted: Any) -> str:
    """The one sentence every attribute that differs on both sides is reported in."""
    return f"{at}: declared {attr} {declared!r} != emitted {emitted!r}; {_REGENERATE}"


def _diff_nesting(at: str, declared: Any, emitted: Any) -> list[str]:
    """An integration field carries its nesting alone (CORE-008 decision 5)."""
    return (
        [] if declared == emitted else [_drift_line(at, "nesting", declared, emitted)]
    )


def _diff_typed(at: str, declared: Any, emitted: dict[str, Any]) -> list[str]:
    """A message field is ``{type, nullable, required}``; one line per attribute."""
    declared = declared if isinstance(declared, dict) else {}
    return [
        _drift_line(at, attr, declared.get(attr), emitted.get(attr))
        for attr in sorted(set(declared) | set(emitted))
        if declared.get(attr) != emitted.get(attr)
    ]


_Entry = Callable[[str, Any, dict[str, Any]], list[str]]


def _diff_catalogue(
    noun: str, state: str, on_disk: Any, live: dict[str, Any], entry: _Entry
) -> list[str]:
    """One line per name on one side only; ``entry`` compares a name on both.

    The section is the plural of its noun: ``messages`` are catalogued,
    ``commands`` registered.
    """
    section = f"{noun}s"
    if not isinstance(on_disk, dict):
        return [f"{section} is not an object; {_REGENERATE}"]
    violations: list[str] = []
    for name in sorted(set(on_disk) | set(live)):
        if name not in live:
            violations.append(
                f"{section}: declared {noun} {name!r} is no longer {state}; {_REGENERATE}"
            )
        elif name not in on_disk:
            violations.append(
                f"{section}: {noun} {name!r} is {state} but not declared; {_REGENERATE}"
            )
        else:
            violations.extend(entry(f"{section}.{name}", on_disk[name], live[name]))
    return violations


def _diff_messages(on_disk: Any, live: dict[str, Any]) -> list[str]:
    """Per-message differences: direction, root and note, then classes per field."""
    return _diff_catalogue("message", "catalogued", on_disk, live, _diff_message_entry)


def _diff_message_entry(at: str, declared: Any, live: dict[str, Any]) -> list[str]:
    """The envelope is an entry like any other: no direction on either side."""
    declared = declared if isinstance(declared, dict) else {}
    drifted = [
        _drift_line(at, key, declared.get(key), live.get(key))
        for key in ("direction", "root", "_note")
        if declared.get(key) != live.get(key)
    ]
    classes = _diff_classes(
        at, declared.get("classes") or {}, live["classes"], _diff_typed
    )
    if "sources" not in live:
        return drifted + classes
    return (
        drifted
        + classes
        + _diff_sources(f"{at}.sources", declared.get("sources"), live["sources"])
    )


def _diff_sources(at: str, declared: Any, live: dict[str, Any]) -> list[str]:
    """Per event source, per kind: a kind on one side only, or its fields drifting."""
    declared = declared if isinstance(declared, dict) else {}
    violations: list[str] = []
    for source in sorted(set(declared) | set(live)):
        listed = declared.get(source) if isinstance(declared.get(source), dict) else {}
        emitted = live.get(source) or {}
        violations.extend(_diff_source_kinds(at, source, listed, emitted))
    return violations


def _diff_source_kinds(at: str, source: str, listed: Any, emitted: Any) -> list[str]:
    """One source's kinds: listed but gone, declared but unlisted, or fields drifting."""
    violations: list[str] = []
    for kind in sorted(set(listed) | set(emitted)):
        if kind not in emitted:
            violations.append(
                f"{at}.{source}: listed kind {kind!r} is no longer declared; "
                f"{_REGENERATE}"
            )
        elif kind not in listed:
            violations.append(
                f"{at}.{source}: kind {kind!r} is declared but not listed; "
                f"{_REGENERATE}"
            )
        elif listed[kind] != emitted[kind]:
            violations.append(
                _drift_line(
                    f"{at}.{source}.{kind}", "fields", listed[kind], emitted[kind]
                )
            )
    return violations


def _diff_commands(on_disk: Any, live: dict[str, Any]) -> list[str]:
    """Per-command differences in the declared surface: params, then result."""
    return _diff_catalogue("command", "registered", on_disk, live, _diff_command_entry)


def _diff_command_entry(at: str, declared: Any, live: dict[str, Any]) -> list[str]:
    declared = declared if isinstance(declared, dict) else {}
    return _diff_params(at, declared.get("params"), live["params"]) + _diff_result(
        f"{at}.result", declared.get("result"), live["result"]
    )


def _diff_result(at: str, declared: Any, live: dict[str, Any] | None) -> list[str]:
    """A result shape is typed like a message: its root, then classes per field."""
    if live is None or not isinstance(declared, dict):
        return [] if declared == live else [_drift_line(at, "shape", declared, live)]
    drifted = (
        []
        if declared.get("root") == live["root"]
        else [_drift_line(at, "root", declared.get("root"), live["root"])]
    )
    return drifted + _diff_classes(
        at, declared.get("classes") or {}, live["classes"], _diff_typed
    )


def _diff_params(at: str, declared: Any, live: dict[str, Any]) -> list[str]:
    """One line per param present on one side only, or validated differently."""
    declared = declared if isinstance(declared, dict) else {}
    emitted = live
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


def _diff_vectors(on_disk: Any, live: dict[str, Any]) -> list[str]:
    """Per signed type: the published vector must be what the live signer writes."""
    return _diff_catalogue("vector", "signed", on_disk, live, _diff_vector_entry)


def _diff_vector_entry(at: str, declared: Any, live: dict[str, Any]) -> list[str]:
    return (
        []
        if declared == live
        else [
            f"{at}: declared vector is not what the live signer writes; {_REGENERATE}"
        ]
    )


# A unit rides in the name (CORE-011 decision 3): one of these words, anywhere.
_UNIT_WORDS = frozenset({"bytes", "seconds", "ms", "gb", "mb", "percent"})
# A version is a label, not a measure: the envelope's ``v`` and register.ok's pick.
_VERSION_NAMES = frozenset({"protocol"})
# A count or a code has no unit: the name itself, or a name ending in ``_<count>``.
_COUNT_NAMES = (
    "object_count",
    "generation",
    "exit_code",
    "http_status",
    "current",
    "total",
    "calls",
    "dropped",
    "from_position",
    "to_position",
    "step_index",
)


def _unit_violations(messages: dict[str, Any]) -> list[str]:
    """One line per numeric payload field that is neither a unit, a count nor a version."""
    return [
        f"messages: numeric field {cls}.{field} carries no unit in its name "
        f"({', '.join('_' + u for u in sorted(_UNIT_WORDS))}) and is not a count; "
        f"rename it"
        for cls, field in _numeric_fields(messages)
        if not (_names_its_measure(field) or field in _VERSION_NAMES)
    ]


def _numeric_fields(messages: dict[str, Any]) -> list[tuple[str, str]]:
    """``(class, field)`` per numeric payload field; a class the acks repeat, once.

    The envelope is skipped: ``v`` is a version, not a measure.
    """
    classes: dict[str, dict[str, Any]] = {}
    for name, entry in messages.items():
        if name != ENVELOPE_ENTRY:
            classes.update(entry["classes"])
    return [
        (cls, field)
        for cls, declared in sorted(classes.items())
        for field, typed in sorted(declared.items())
        if _is_numeric(typed["type"])
    ]


def _is_numeric(wire_type: Any) -> bool:
    types = wire_type if isinstance(wire_type, list) else [wire_type]
    return any(t in ("integer", "number") for t in types)


def _names_its_measure(field: str) -> bool:
    """A unit word anywhere in the name, or a count name, bare or as a suffix."""
    return bool(set(field.split("_")) & _UNIT_WORDS) or any(
        field == c or field.endswith(f"_{c}") for c in _COUNT_NAMES
    )


def _diff_v1_digest(on_disk: dict[str, Any], live: dict[str, Any]) -> list[str]:
    """``digest_covers`` names the v1 section in the file; a wrong name is its own line."""
    covers = on_disk.get("digest_covers")
    naming = (
        []
        if covers == DIGEST_COVERS
        else [f"digest_covers is {covers!r}, expected {DIGEST_COVERS!r}; {_REGENERATE}"]
    )
    reproduced = canonical_digest(on_disk.get(DIGEST_COVERS))
    return naming + _diff_digest("digest", DIGEST_COVERS, reproduced, on_disk, live)


def _diff_digest(
    label: str,
    covers: Any,
    reproduced: str,
    on_disk: dict[str, Any],
    live: dict[str, Any],
) -> list[str]:
    """Both digests answer the same two questions: does the file reproduce its
    own value from what it covers, and does that value match the live classes."""
    declared = on_disk.get(label)
    violations: list[str] = []
    if reproduced != declared:
        violations.append(
            f"{WIRE_CONTRACT_PATH.name} carries a {label} that does not match "
            f"{covers!r}, the content it covers, so a consumer holding only this file "
            f"cannot reproduce it; {_REGENERATE}"
        )
    if declared != live[label]:
        violations.append(
            f"declared {label} {declared} != live {label} {live[label]}; {_REGENERATE}"
        )
    return violations
