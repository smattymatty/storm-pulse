"""The param-schema walkers: what a declaration may say, and what a decoded blob
may break. ``validate_params`` wires them in; tests/test_commands.py pins that seam."""

from __future__ import annotations

import pytest

from stormpulse.config import ParamDef
from stormpulse.config.param_schema import schema_declaration_error, schema_violation

# ---------------------------------------------------------------------------
# schema_declaration_error: the shape of a shape
# ---------------------------------------------------------------------------

_LIST_OF_IDS = {"type": "array", "items": {"type": "string", "pattern": "[a-f0-9]+"}}


def test_param_schema_needs_max_bytes_and_excludes_pattern() -> None:
    with pytest.raises(ValueError, match="needs max_bytes"):
        ParamDef(placeholder="blob", default=None, pattern=".*", schema=_LIST_OF_IDS)
    with pytest.raises(ValueError, match="needs max_bytes"):
        ParamDef(
            placeholder="blob",
            default=None,
            max_bytes=10,
            pattern=".*",
            schema=_LIST_OF_IDS,
        )
    ParamDef(placeholder="blob", default=None, max_bytes=10, schema=_LIST_OF_IDS)


@pytest.mark.parametrize(
    ("schema", "fragment"),
    [
        ("not a dict", "shape must be an object"),
        ({"type": "float"}, "type must be one of"),
        ({"type": "string", "items": {}}, "unknown shape keys ['items']"),
        ({"type": "string", "nullable": "yes"}, "nullable must be a boolean"),
        ({"type": "string", "max_bytes": True}, "max_bytes must be an integer"),
        ({"type": "string", "pattern": "["}, "pattern is not valid regex"),
        ({"type": "string", "pattern": 5}, "pattern is not valid regex"),
        (
            {
                "type": "object",
                "entries": {"key_pattern": 5, "value": {"type": "string"}},
            },
            "key_pattern is not valid regex",
        ),
        ({"type": "array"}, "array needs items"),
        ({"type": "array", "items": {"type": "nope"}}, "schema.items: type must be"),
        ({"type": "object", "keys": []}, "keys must be an object"),
        (
            {"type": "object", "keys": {"a": {"type": "string"}}, "required": ["b"]},
            "required must list",
        ),
        (
            {"type": "object", "entries": {"value": {"type": "string"}, "extra": 1}},
            "key_pattern and value only",
        ),
        (
            {
                "type": "object",
                "entries": {"key_pattern": "(", "value": {"type": "string"}},
            },
            "key_pattern is not valid regex",
        ),
        ({"type": "object", "entries": {"key_pattern": "x"}}, "entries needs value"),
        (
            {"type": "object", "keys": {"a": {"type": "integer", "min": "0"}}},
            "schema.keys['a']: min must be an integer",
        ),
    ],
)
def test_param_schema_declaration_is_walked(schema: object, fragment: str) -> None:
    err = schema_declaration_error(schema)
    assert err is not None and fragment in err
    with pytest.raises(ValueError, match="ParamDef 'blob': "):
        ParamDef(placeholder="blob", default=None, max_bytes=10, schema=schema)  # type: ignore[arg-type]


def test_param_schema_well_formed_shapes_construct() -> None:
    assert (
        schema_declaration_error(
            {
                "type": "object",
                "keys": {
                    "id": {"type": "string", "pattern": "[a-f0-9]{16,64}"},
                    "n": {"type": "integer", "min": 0, "max": 10, "nullable": True},
                    "on": {"type": "boolean"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "max_items": 3,
                    },
                },
                "required": ["id"],
                "entries": {
                    "key_pattern": "x-.*",
                    "value": {"type": "string", "max_bytes": 8},
                },
            }
        )
        is None
    )


# ---------------------------------------------------------------------------
# schema_violation: the first way a decoded value breaks its shape
# ---------------------------------------------------------------------------

_RULES_SCHEMA = {
    "type": "array",
    "max_items": 2,
    "items": {
        "type": "object",
        "keys": {
            "ID": {"type": "string", "pattern": "[a-z]+", "nullable": True},
            "MaxAgeSeconds": {"type": "integer", "min": 0, "max": 86400},
            "AllowedOrigin": {
                "type": "array",
                "items": {"type": "string", "max_bytes": 8},
            },
            "Secure": {"type": "boolean"},
        },
        "required": ["AllowedOrigin"],
    },
}


def test_violation_is_none_for_a_conforming_value() -> None:
    value = [{"ID": None, "MaxAgeSeconds": 60, "AllowedOrigin": ["*"], "Secure": True}]
    assert schema_violation(value, _RULES_SCHEMA) is None


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ({"a": 1}, "$: expected array, got dict"),
        ([[], [], []], "$: 3 items, exceeds max_items=2"),
        ([{"ID": "x"}], "$[0]: missing required key 'AllowedOrigin'"),
        ([{"AllowedOrigin": [], "Extra": 1}], "$[0]: unexpected key 'Extra'"),
        (
            [{"AllowedOrigin": [], "ID": "X1"}],
            "$[0].ID: does not match pattern '[a-z]+'",
        ),
        (
            [{"AllowedOrigin": [], "MaxAgeSeconds": -1}],
            "$[0].MaxAgeSeconds: below min=0",
        ),
        (
            [{"AllowedOrigin": [], "MaxAgeSeconds": 90000}],
            "$[0].MaxAgeSeconds: above max=86400",
        ),
        (
            [{"AllowedOrigin": [], "MaxAgeSeconds": True}],
            "$[0].MaxAgeSeconds: expected integer, got bool",
        ),
        (
            [{"AllowedOrigin": [], "MaxAgeSeconds": None}],
            "$[0].MaxAgeSeconds: expected integer, got null",
        ),
        ([{"AllowedOrigin": "*"}], "$[0].AllowedOrigin: expected array, got str"),
        (
            [{"AllowedOrigin": ["*", "toolongorigin"]}],
            "$[0].AllowedOrigin[1]: exceeds max_bytes=8",
        ),
        (
            [{"AllowedOrigin": [], "Secure": "yes"}],
            "$[0].Secure: expected boolean, got str",
        ),
    ],
)
def test_violation_names_the_path_and_the_rule(value: object, message: str) -> None:
    assert schema_violation(value, _RULES_SCHEMA) == message


def test_violation_walks_map_entries() -> None:
    schema = {
        "type": "object",
        "keys": {"version": {"type": "integer"}},
        "entries": {"key_pattern": "[a-z]+", "value": {"type": "string"}},
    }
    assert schema_violation({"version": 1, "abc": "x"}, schema) is None
    assert schema_violation({"A-1": "x"}, schema) == (
        "$: key 'A-1' does not match key_pattern '[a-z]+'"
    )
    assert schema_violation({"abc": 1}, schema) == "$.abc: expected string, got int"


def test_violation_never_echoes_the_value() -> None:
    message = schema_violation({"LeakedKey": "LeakedValue"}, {"type": "object"})
    assert message is not None
    assert "LeakedValue" not in message
