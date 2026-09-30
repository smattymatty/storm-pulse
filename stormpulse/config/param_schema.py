"""The declared shape of a JSON-valued command param, and the two walkers on it.

A shape is plain JSON, so it publishes into the wire contract and copies
through the adapter loader verbatim. ``schema_declaration_error`` runs once at
``ParamDef`` construction; ``schema_violation`` runs at dispatch on the decoded
blob. Standard library only; the messages carry paths and rules, never values.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

ParamSchema = dict[str, Any]

_NULLABLE = "nullable"
_KEY_PATTERN = "key_pattern"
_INT_KEYS = ("max_items", "max_bytes", "min", "max")

_SCHEMA_KEYS: dict[str, frozenset[str]] = {
    "object": frozenset({"keys", "required", "entries"}),
    "array": frozenset({"items", "max_items"}),
    "string": frozenset({"pattern", "max_bytes"}),
    "integer": frozenset({"min", "max"}),
    "boolean": frozenset(),
}


def _first(*problems: str | None) -> str | None:
    """The first rule that failed, in the order the rules are written."""
    return next((p for p in problems if p is not None), None)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def schema_declaration_error(schema: Any, path: str = "schema") -> str | None:
    """Why ``schema`` is not a well-formed shape, or None. Nodes:
    ``{"type": "object", "keys": {name: shape}, "required": [name], "entries":
    {"key_pattern": regex, "value": shape}}`` (no entries: unknown keys reject);
    ``{"type": "array", "items": shape, "max_items": int}``; ``{"type": "string",
    "pattern": regex, "max_bytes": int}``; ``{"type": "integer", "min": int,
    "max": int}``; ``{"type": "boolean"}``. Any node may be ``"nullable"``; fullmatch."""
    if not isinstance(schema, dict):
        return f"{path}: shape must be an object"
    kind = schema.get("type")
    if kind not in _SCHEMA_KEYS:
        return f"{path}: type must be one of {sorted(_SCHEMA_KEYS)}, got {kind!r}"
    problem = _node_declaration_error(schema, kind, path)
    if problem is None and kind == "object":
        problem = _object_declaration_error(schema, path)
    if problem is None and kind == "array":
        problem = _items_declaration_error(schema, path)
    return problem


def _node_declaration_error(schema: dict[str, Any], kind: str, path: str) -> str | None:
    """The rules every node shares: known keys, nullable, integer bounds, pattern."""
    unknown = sorted(set(schema) - _SCHEMA_KEYS[kind] - {"type", _NULLABLE})
    bad_ints = [k for k in _INT_KEYS if k in schema and not _is_int(schema[k])]
    pattern_err = _regex_error(schema["pattern"]) if "pattern" in schema else None
    return _first(
        f"{path}: unknown shape keys {unknown} for type {kind!r}" if unknown else None,
        None
        if isinstance(schema.get(_NULLABLE, False), bool)
        else f"{path}: {_NULLABLE} must be a boolean",
        f"{path}: {bad_ints[0]} must be an integer" if bad_ints else None,
        f"{path}: pattern {pattern_err}" if pattern_err is not None else None,
    )


def _items_declaration_error(schema: dict[str, Any], path: str) -> str | None:
    if "items" not in schema:
        return f"{path}: array needs items"
    return schema_declaration_error(schema["items"], f"{path}.items")


def _regex_error(pattern: Any) -> str | None:
    try:
        re.compile(pattern)
    except (re.error, TypeError) as exc:
        detail = getattr(exc, "msg", "pattern must be a string")
    else:
        return None
    return f"is not valid regex: {detail}"


def _object_declaration_error(schema: dict[str, Any], path: str) -> str | None:
    keys = schema.get("keys", {})
    if not isinstance(keys, dict):
        return f"{path}: keys must be an object"
    problem = _first(
        *(
            schema_declaration_error(shape, f"{path}.keys[{name!r}]")
            for name, shape in keys.items()
        )
    )
    required = schema.get("required", [])
    if problem is None and (
        not isinstance(required, list) or any(r not in keys for r in required)
    ):
        problem = f"{path}: required must list names declared in keys"
    if problem is None and schema.get("entries") is not None:
        problem = _entries_declaration_error(schema["entries"], path)
    return problem


def _entries_declaration_error(entries: Any, path: str) -> str | None:
    if not isinstance(entries, dict) or set(entries) - {_KEY_PATTERN, "value"}:
        return f"{path}: entries takes {_KEY_PATTERN} and value only"
    pattern_err = (
        _regex_error(entries[_KEY_PATTERN]) if _KEY_PATTERN in entries else None
    )
    if pattern_err is not None:
        return f"{path}: {_KEY_PATTERN} {pattern_err}"
    if "value" not in entries:
        return f"{path}: entries needs value"
    return schema_declaration_error(entries["value"], f"{path}.entries.value")


def schema_violation(value: Any, schema: ParamSchema, path: str = "$") -> str | None:
    """The first way a decoded JSON ``value`` breaks ``schema``, or None.

    Messages carry the JSON path and the rule, never the offending value:
    they ride error payloads and logs.
    """
    kind = schema["type"]
    if value is None:
        return None if schema.get(_NULLABLE) else f"{path}: expected {kind}, got null"
    return _VIOLATION_BY_TYPE[kind](value, schema, path)


def _object_violation(value: Any, schema: ParamSchema, path: str) -> str | None:
    if not isinstance(value, dict):
        return f"{path}: expected object, got {type(value).__name__}"
    missing = next((n for n in schema.get("required", []) if n not in value), None)
    if missing is not None:
        return f"{path}: missing required key {missing!r}"
    return _first(
        *(_entry_violation(name, item, schema, path) for name, item in value.items())
    )


def _entry_violation(
    name: str, item: Any, schema: ParamSchema, path: str
) -> str | None:
    """One object entry against its declared key, or the entries rule."""
    keys: dict[str, ParamSchema] = schema.get("keys", {})
    entries = schema.get("entries")
    if name in keys:
        shape = keys[name]
    elif entries is None:
        return f"{path}: unexpected key {name!r}"
    else:
        key_pattern = entries.get(_KEY_PATTERN)
        if key_pattern is not None and not re.fullmatch(key_pattern, name):
            return f"{path}: key {name!r} does not match {_KEY_PATTERN} {key_pattern!r}"
        shape = entries["value"]
    return schema_violation(item, shape, f"{path}.{name}")


def _array_violation(value: Any, schema: ParamSchema, path: str) -> str | None:
    if not isinstance(value, list):
        return f"{path}: expected array, got {type(value).__name__}"
    if "max_items" in schema and len(value) > schema["max_items"]:
        return f"{path}: {len(value)} items, exceeds max_items={schema['max_items']}"
    return _first(
        *(
            schema_violation(item, schema["items"], f"{path}[{i}]")
            for i, item in enumerate(value)
        )
    )


def _string_violation(value: Any, schema: ParamSchema, path: str) -> str | None:
    if not isinstance(value, str):
        return f"{path}: expected string, got {type(value).__name__}"
    if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
        return f"{path}: does not match pattern {schema['pattern']!r}"
    if "max_bytes" in schema and len(value.encode("utf-8")) > schema["max_bytes"]:
        return f"{path}: exceeds max_bytes={schema['max_bytes']}"
    return None


def _integer_violation(value: Any, schema: ParamSchema, path: str) -> str | None:
    if not _is_int(value):
        return f"{path}: expected integer, got {type(value).__name__}"
    if "min" in schema and value < schema["min"]:
        return f"{path}: below min={schema['min']}"
    if "max" in schema and value > schema["max"]:
        return f"{path}: above max={schema['max']}"
    return None


def _boolean_violation(value: Any, schema: ParamSchema, path: str) -> str | None:
    if not isinstance(value, bool):
        return f"{path}: expected boolean, got {type(value).__name__}"
    return None


# One checker per declared type; the declaration walk guarantees the key exists.
_VIOLATION_BY_TYPE: dict[str, Callable[[Any, ParamSchema, str], str | None]] = {
    "object": _object_violation,
    "array": _array_violation,
    "string": _string_violation,
    "integer": _integer_violation,
    "boolean": _boolean_violation,
}
