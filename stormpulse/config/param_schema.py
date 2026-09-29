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

_SCHEMA_KEYS: dict[str, frozenset[str]] = {
    "object": frozenset({"keys", "required", "entries"}),
    "array": frozenset({"items", "max_items"}),
    "string": frozenset({"pattern", "max_bytes"}),
    "integer": frozenset({"min", "max"}),
    "boolean": frozenset(),
}


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
    unknown = set(schema) - _SCHEMA_KEYS[kind] - {"type", "nullable"}
    if unknown:
        return f"{path}: unknown shape keys {sorted(unknown)} for type {kind!r}"
    if not isinstance(schema.get("nullable", False), bool):
        return f"{path}: nullable must be a boolean"
    for key in ("max_items", "max_bytes", "min", "max"):
        if key in schema and (
            isinstance(schema[key], bool) or not isinstance(schema[key], int)
        ):
            return f"{path}: {key} must be an integer"
    if "pattern" in schema and (err := _regex_error(schema["pattern"])) is not None:
        return f"{path}: pattern {err}"
    if kind == "object":
        return _object_declaration_error(schema, path)
    if kind == "array":
        if "items" not in schema:
            return f"{path}: array needs items"
        return schema_declaration_error(schema["items"], f"{path}.items")
    return None


def _regex_error(pattern: Any) -> str | None:
    try:
        re.compile(pattern)
    except (re.error, TypeError) as exc:
        return f"is not valid regex: {exc}"
    return None


def _object_declaration_error(schema: dict[str, Any], path: str) -> str | None:
    keys = schema.get("keys", {})
    if not isinstance(keys, dict):
        return f"{path}: keys must be an object"
    for name, shape in keys.items():
        err = schema_declaration_error(shape, f"{path}.keys[{name!r}]")
        if err is not None:
            return err
    required = schema.get("required", [])
    if not isinstance(required, list) or any(r not in keys for r in required):
        return f"{path}: required must list names declared in keys"
    entries = schema.get("entries")
    if entries is None:
        return None
    if not isinstance(entries, dict) or set(entries) - {"key_pattern", "value"}:
        return f"{path}: entries takes key_pattern and value only"
    if (
        "key_pattern" in entries
        and (err := _regex_error(entries["key_pattern"])) is not None
    ):
        return f"{path}: key_pattern {err}"
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
        return None if schema.get("nullable") else f"{path}: expected {kind}, got null"
    return _VIOLATION_BY_TYPE[kind](value, schema, path)


def _object_violation(value: Any, schema: ParamSchema, path: str) -> str | None:
    if not isinstance(value, dict):
        return f"{path}: expected object, got {type(value).__name__}"
    keys: dict[str, ParamSchema] = schema.get("keys", {})
    for name in schema.get("required", []):
        if name not in value:
            return f"{path}: missing required key {name!r}"
    entries = schema.get("entries")
    for name, item in value.items():
        if name in keys:
            shape = keys[name]
        elif entries is None:
            return f"{path}: unexpected key {name!r}"
        else:
            key_pattern = entries.get("key_pattern")
            if key_pattern is not None and not re.fullmatch(key_pattern, name):
                return (
                    f"{path}: key {name!r} does not match key_pattern {key_pattern!r}"
                )
            shape = entries["value"]
        err = schema_violation(item, shape, f"{path}.{name}")
        if err is not None:
            return err
    return None


def _array_violation(value: Any, schema: ParamSchema, path: str) -> str | None:
    if not isinstance(value, list):
        return f"{path}: expected array, got {type(value).__name__}"
    if "max_items" in schema and len(value) > schema["max_items"]:
        return f"{path}: {len(value)} items, exceeds max_items={schema['max_items']}"
    for i, item in enumerate(value):
        err = schema_violation(item, schema["items"], f"{path}[{i}]")
        if err is not None:
            return err
    return None


def _string_violation(value: Any, schema: ParamSchema, path: str) -> str | None:
    if not isinstance(value, str):
        return f"{path}: expected string, got {type(value).__name__}"
    if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
        return f"{path}: does not match pattern {schema['pattern']!r}"
    if "max_bytes" in schema and len(value.encode("utf-8")) > schema["max_bytes"]:
        return f"{path}: exceeds max_bytes={schema['max_bytes']}"
    return None


def _integer_violation(value: Any, schema: ParamSchema, path: str) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int):
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
