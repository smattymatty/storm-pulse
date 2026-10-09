"""The reflection walkers that declare a dataclass tree's wire shape (CORE-008).

``dataclass_wire_shape`` is names and nesting; ``dataclass_wire_types`` adds
JSON types, nullability and optionality. ``agent/wire_contract.py`` renders
both into the artifact; ``from_dict`` reads ``wire_type_name``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import MISSING, Field, fields, is_dataclass
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints


def _wire_fields(cls: type) -> tuple[Field[Any], ...]:
    """The fields a class writes to the wire: every one not marked ``wire=False``."""
    return tuple(f for f in fields(cls) if f.metadata.get("wire", True))


def _nested_dataclass(annotation: Any) -> type | None:
    """The dataclass a field's annotation ultimately contains, or None.

    Unwraps one level of container or union at a time (``list[X]``,
    ``tuple[X, ...]``, ``dict[str, X]``, ``X | None``) because the shapes we
    publish nest exactly that way. A ``tuple[str, ...]`` yields None: a
    container of scalars is a leaf, not a nesting.
    """
    if is_dataclass(annotation) and isinstance(annotation, type):
        return annotation
    for arg in get_args(annotation):
        if arg is type(None):
            continue
        found = _nested_dataclass(arg)
        if found is not None:
            return found
    return None


def dataclass_wire_shape(root: type) -> dict[str, dict[str, str | None]]:
    """Field names and nesting of *root* and every dataclass reachable from it:
    ``{class name: {field name: nested class name or None}}``, what ``asdict``
    publishes. Names and nesting only, never types, units or meaning (CORE-008
    decision 5): a rename shows up, a unit change does not; ``dataclass_wire_types``
    is the typed walker. Traversal order; hashers sort it.
    """
    return {
        cls.__name__: _shape_fields(cls, hints)
        for cls, hints in _reachable_dataclasses(root, "dataclass_wire_shape")
    }


def _shape_fields(cls: type, hints: dict[str, Any]) -> dict[str, str | None]:
    """One class's ``{field: nested class name or None}``."""
    shape: dict[str, str | None] = {}
    for f in _wire_fields(cls):
        nested = _nested_dataclass(hints[f.name])
        shape[f.name] = nested.__name__ if nested is not None else None
    return shape


def _reachable_dataclasses(
    root: type, caller: str
) -> Iterator[tuple[type, dict[str, Any]]]:
    """Every dataclass reachable from *root* with its resolved hints, each once.

    Root first, nested classes as a stack reaches them: the traversal both walkers share.
    """
    if not (is_dataclass(root) and isinstance(root, type)):
        raise TypeError(f"{caller} requires a dataclass type, got {root!r}")
    seen: set[str] = set()
    pending: list[type] = [root]
    while pending:
        cls = pending.pop()
        if cls.__name__ in seen:
            continue
        seen.add(cls.__name__)
        hints = get_type_hints(cls)
        yield cls, hints
        for f in _wire_fields(cls):
            nested = _nested_dataclass(hints[f.name])
            if nested is not None:
                pending.append(nested)


# Matched by subclass, so a StrEnum is a string; bool first because it is an int.
_SCALAR_WIRE_TYPES: dict[type, str] = {
    bool: "boolean",
    int: "integer",
    float: "number",
    str: "string",
}


def wire_type_name(annotation: Any) -> str:
    """One JSON type name, or a dataclass name, for a non-union annotation.

    Containers carry no element type: that is the declared vocabulary
    (CORE-011 decision 3), not a shortcut.
    """
    if is_dataclass(annotation) and isinstance(annotation, type):
        return annotation.__name__
    origin = get_origin(annotation)
    if isinstance(origin, type) and issubclass(origin, Mapping):
        return "object"
    if isinstance(origin, type) and issubclass(origin, (list, tuple)):
        return "array"
    for scalar, name in _SCALAR_WIRE_TYPES.items():
        if isinstance(annotation, type) and issubclass(annotation, scalar):
            return name
    raise TypeError(f"no wire type for annotation {annotation!r}")


def _wire_type(annotation: Any) -> tuple[str | list[str], bool]:
    """The JSON type a field serializes to, and whether None is allowed.

    A union of several (``int | str``) is a sorted list; ``X | None`` is X, nullable.
    """
    if get_origin(annotation) not in (UnionType, Union):
        return wire_type_name(annotation), False
    members = [a for a in get_args(annotation) if a is not type(None)]
    names = sorted(wire_type_name(m) for m in members)
    nullable = len(members) < len(get_args(annotation))
    return (names[0] if len(names) == 1 else names), nullable


def dataclass_wire_types(
    root: type, *, formatted: Mapping[str, str] | None = None
) -> dict[str, dict[str, dict[str, Any]]]:
    """Field types, nullability and optionality of *root* and every dataclass it reaches.

    ``{class name: {field name: {"type", "nullable", "required"}}}``: ``type``
    is the JSON type (or class name, or sorted list for a union), ``nullable``
    is ``X | None``, ``required`` is "no default"; ``formatted`` overrides the type
    a serializer rewrites (``Envelope.ts``). Same order as ``dataclass_wire_shape``.
    """
    return {
        cls.__name__: _typed_fields(cls, hints, formatted or {})
        for cls, hints in _reachable_dataclasses(root, "dataclass_wire_types")
    }


def _typed_fields(
    cls: type, hints: dict[str, Any], formatted: Mapping[str, str]
) -> dict[str, dict[str, Any]]:
    """One class's ``{field: {type, nullable, required}}``; a formatted field is never null."""
    typed: dict[str, dict[str, Any]] = {}
    for f in _wire_fields(cls):
        wire_type: str | list[str]
        if f.name in formatted:
            wire_type, nullable = formatted[f.name], False
        else:
            wire_type, nullable = _wire_type(hints[f.name])
        typed[f.name] = {
            "type": wire_type,
            "nullable": nullable,
            "required": f.default is MISSING and f.default_factory is MISSING,
        }
    return typed
