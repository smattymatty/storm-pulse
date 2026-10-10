"""Codec: the version gate and the reflection that turns a dict into a payload.

Nothing here knows a payload class by name; ``payloads`` declares them and
``WirePayload.from_dict`` is the one door in. Raises ``ProtocolError`` only.
"""

from __future__ import annotations

from dataclasses import MISSING, Field, fields, is_dataclass
from functools import cache
from types import UnionType
from typing import (
    Any,
    ClassVar,
    Self,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
)

#: Envelope versions this agent speaks, oldest first; the last is what it
#: writes. Negotiated at register (CORE-011 d2). A frame outside it is refused.
SPEAKS: tuple[int, ...] = (2,)


class ProtocolError(Exception):
    """Raised when a message fails validation or parsing.

    ``reason`` is a closed enum naming why, the way ``AuthError`` does;
    the ``error`` reply quotes it, never the human message.
    """

    #: Every value ``reason`` may take. A refusal outside this set is a bug.
    REASONS = frozenset(  # skylos: ignore - documented closed enum; the contract, not a consumer
        {"unsupported_version", "unknown_type", "bad_envelope", "duplicate_request"}
    )

    def __init__(
        self, message: str, *, reason: str = "bad_envelope", msg_type: str | None = None
    ) -> None:
        super().__init__(message)
        self.reason = reason
        #: The refused frame's ``type`` when one was readable; the reply quotes it.
        self.msg_type = msg_type


def check_version(v: Any) -> int:
    """The one place a version meets ``SPEAKS``, an envelope's ``v`` or the one
    ``register.ok`` picked; bool is not an int here. The refusal names both sides."""
    if type(v) is not int or v not in SPEAKS:
        raise ProtocolError(
            f"Unsupported protocol version: {v!r}; this agent speaks {list(SPEAKS)}",
            reason="unsupported_version",
        )
    return v


class WirePayload:
    """Base of every payload class: one ``from_dict``, so a table can hold them all."""

    __slots__ = ()
    # Every subclass is a dataclass; declared so ``asdict`` accepts the base.
    __dataclass_fields__: ClassVar[dict[str, Field[Any]]]

    @classmethod
    def from_dict(cls, data: Any) -> Self:
        return _payload_from_dict(cls, data)


def _payload_from_dict[T](cls: type[T], data: Any) -> T:
    """Validate required fields and construct ``cls`` from ``data``; each present
    field is decoded against its own annotation, nesting included."""
    if not isinstance(data, dict):
        raise ProtocolError(f"{cls.__name__}: expected dict, got {type(data).__name__}")

    cls_fields = fields(cls)  # type: ignore[arg-type]
    required = {
        f.name
        for f in cls_fields
        if f.default is MISSING and f.default_factory is MISSING
    }

    missing_fields = required - data.keys()
    if missing_fields:
        raise ProtocolError(f"{cls.__name__}: missing fields: {missing_fields}")

    owner = cast(type, cls)
    hints = _payload_hints(owner)
    filtered = {
        f.name: _decode(owner, f.name, hints[f.name], data[f.name])
        for f in cls_fields
        if f.name in data
    }
    return cls(**filtered)


@cache
def _payload_hints(cls: type) -> dict[str, Any]:
    return get_type_hints(cls)


def _decode(owner: type, name: str, annotation: Any, value: Any) -> Any:
    """One wire value against one annotation, dispatched once: a union takes
    the first arm that decodes, a container maps its items, a dataclass is
    rebuilt, a scalar is checked. Refuses naming ``Owner.field``."""
    if annotation is Any:
        return value
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (UnionType, Union):
        if value is None and type(None) in args:
            return None
        arms = [a for a in args if a is not type(None)]
        for arm in arms[:-1]:
            try:
                return _decode(owner, name, arm, value)
            except ProtocolError:
                pass
        return _decode(owner, name, arms[-1], value)
    if origin is list:
        if not isinstance(value, list):
            raise ProtocolError(
                f"{owner.__name__}.{name}: expected list, got {type(value).__name__}"
            )
        return [_decode(owner, name, args[0], item) for item in value]
    if origin is dict:
        if not isinstance(value, dict):
            raise ProtocolError(
                f"{owner.__name__}.{name}: expected dict, got {type(value).__name__}"
            )
        return {
            _decode(owner, name, args[0], k): _decode(owner, name, args[1], v)
            for k, v in value.items()
        }
    if is_dataclass(annotation) and isinstance(annotation, type):
        return _payload_from_dict(annotation, value)
    if _is_scalar(annotation, value):
        return value
    raise ProtocolError(
        f"{owner.__name__}.{name}: invalid field type {type(value).__name__}"
    )


def _is_scalar(annotation: Any, value: Any) -> bool:
    """The declared JSON scalar, never interpreted: bool is not an int, an int
    may stand as a float, a StrEnum field reads as its string."""
    if annotation in (bool, int):
        return type(value) is annotation
    if annotation is float:
        return type(value) in (int, float)
    if isinstance(annotation, type) and issubclass(annotation, str):
        return isinstance(value, str)
    return isinstance(value, annotation)
