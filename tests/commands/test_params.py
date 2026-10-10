"""Command params: ``validate_params`` over patterns, byte caps and schemas,
``ParamDef``'s own construction rules, and the secrets kept off event context.
"""

from __future__ import annotations

from typing import Any

import pytest

from stormpulse.agent.wire_contract import wire_contract_commands
from stormpulse.commands import (
    CommandSpec,
    ParamValidationError,
    non_secret_params,
    validate_params,
)
from stormpulse.config import ParamDef

# -- validate_params ---------------------------------------------------------


def _cmd_with_params(**params: ParamDef) -> CommandSpec:
    """Helper to build a CommandSpec with params."""
    return CommandSpec(
        group="test",
        command=["/bin/true"],
        timeout=10,
        params=params,
    )


def test_validate_params_no_params_defined() -> None:
    cmd = _cmd_with_params()
    assert validate_params(cmd, {}) == {}


def test_validate_params_uses_defaults() -> None:
    cmd = _cmd_with_params(
        service=ParamDef(placeholder="service", default="web", pattern="[a-z]+"),
    )
    assert validate_params(cmd, {}) == {"service": "web"}


def test_validate_params_overrides_default() -> None:
    cmd = _cmd_with_params(
        service=ParamDef(placeholder="service", default="web", pattern="[a-z]+"),
    )
    assert validate_params(cmd, {"service": "celery"}) == {"service": "celery"}


def test_validate_params_unknown_param_raises() -> None:
    cmd = _cmd_with_params(
        service=ParamDef(placeholder="service", default="web", pattern="[a-z]+"),
    )
    with pytest.raises(ParamValidationError, match="Unknown"):
        validate_params(cmd, {"bogus": "value"})


def test_validate_params_pattern_mismatch_raises() -> None:
    cmd = _cmd_with_params(
        service=ParamDef(placeholder="service", default="web", pattern="[a-z]+"),
    )
    with pytest.raises(ParamValidationError, match="pattern"):
        validate_params(cmd, {"service": "INVALID!!!"})


def test_validate_params_secret_pattern_mismatch_withholds_value() -> None:
    """A failing secret value never rides the error message (it lands in logs)."""
    cmd = _cmd_with_params(
        secret_access_key=ParamDef(
            placeholder="secret_access_key",
            default=None,
            pattern=r".+",
            secret=True,
        ),
    )
    leaked = "SuperSecretValue123\n"
    with pytest.raises(ParamValidationError, match="withheld") as excinfo:
        validate_params(cmd, {"secret_access_key": leaked})
    assert "SuperSecretValue123" not in str(excinfo.value)


def test_validate_params_keeps_a_value_naming_another_param() -> None:
    """The MAC signs the params as canonical JSON (CORE-011 d4), so a value
    folding ``&other=`` is just a value: nothing to refuse any more."""
    cmd = _cmd_with_params(
        dst_key=ParamDef(
            placeholder="dst_key", default=None, max_bytes=256, secret=True
        ),
        src_id=ParamDef(placeholder="src_id", default=None, pattern="[A-Z0-9]+"),
    )
    value = "S3CR3T&src_id=GK123"
    assert validate_params(cmd, {"dst_key": value}) == {"dst_key": value}


@pytest.mark.parametrize("value", [7, True, None, ["web"], {"name": "web"}])
def test_validate_params_pattern_param_refuses_a_non_string(value: Any) -> None:
    """A pattern or byte cap declares a string; any other JSON type is refused
    before the regex ever runs (CORE-011 d9)."""
    cmd = _cmd_with_params(
        service=ParamDef(placeholder="service", default=None, pattern="[a-z]+"),
        apply=ParamDef(placeholder="apply", default=None, max_bytes=256),
    )
    for name in ("service", "apply"):
        with pytest.raises(ParamValidationError, match="expected string") as excinfo:
            validate_params(cmd, {name: value})
        assert f"Param {name!r} is {type(value).__name__}" in str(excinfo.value)


def test_validate_params_secret_non_string_rides_no_value() -> None:
    cmd = _cmd_with_params(
        secret_key=ParamDef(
            placeholder="secret_key", default=None, pattern=".+", secret=True
        ),
    )
    with pytest.raises(ParamValidationError) as excinfo:
        validate_params(cmd, {"secret_key": {"LeakedKey": "LeakedValue"}})
    assert "Leaked" not in str(excinfo.value)


def test_validate_params_none_default_no_override_skips() -> None:
    """When default is None and no runtime override, the param is skipped.

    The config-level placeholder (e.g. docker_service_name) provides the
    fallback value during _resolve_command.
    """
    cmd = _cmd_with_params(
        service=ParamDef(placeholder="service", default=None, pattern="[a-z]+"),
    )
    assert validate_params(cmd, {}) == {}


def test_validate_params_none_default_with_override() -> None:
    cmd = _cmd_with_params(
        service=ParamDef(placeholder="service", default=None, pattern="[a-z]+"),
    )
    assert validate_params(cmd, {"service": "celery"}) == {"service": "celery"}


# -- schema validation (JSON blobs) ------------------------------------------


_RULES_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "keys": {"AllowedOrigin": {"type": "array", "items": {"type": "string"}}},
        "required": ["AllowedOrigin"],
    },
}


def _rules_param(default: Any = None, **overrides: Any) -> ParamDef:
    return ParamDef(
        placeholder="rules",
        default=default,
        max_bytes=4096,
        schema=_RULES_SCHEMA,
        **overrides,
    )


def _rules_cmd(**overrides: Any) -> CommandSpec:
    return _cmd_with_params(rules=_rules_param(**overrides))


def test_validate_params_schema_accepts_a_conforming_value_verbatim() -> None:
    value = [{"AllowedOrigin": ["*"]}]
    validated = validate_params(_rules_cmd(), {"rules": value})
    assert validated == {"rules": value} and validated["rules"] is value


@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        (
            '[{"AllowedOrigin": ["*"]}]',
            "does not match schema: $: expected array, got str",
        ),
        (
            {"AllowedOrigin": ["*"]},
            "does not match schema: $: expected array, got dict",
        ),
        (
            [{"ID": "x"}],
            "does not match schema: $[0]: missing required key 'AllowedOrigin'",
        ),
    ],
)
def test_validate_params_schema_refuses_a_string_or_the_wrong_shape(
    value: Any, fragment: str
) -> None:
    """The JSON value is walked as it rides: a string holding JSON is not decoded,
    it is a string where an array was declared (CORE-011 d9). The walker's own
    table lives in tests/test_param_schema.py; this pins the seam."""
    with pytest.raises(ParamValidationError, match="Param 'rules' ") as excinfo:
        validate_params(_rules_cmd(), {"rules": value})
    assert fragment in str(excinfo.value)


def test_validate_params_schema_runs_after_the_byte_cap() -> None:
    """The cap reads the value's canonical encoding: 4096 bytes is 4096 bytes of
    ``[{"AllowedOrigin":["..."]}]``, however the sender spaced it."""
    origin = "é" * 700  # 2 UTF-8 bytes each; 6 once canonical_json escapes it
    rules = [{"AllowedOrigin": [origin]}]
    with pytest.raises(
        ParamValidationError, match="is 4224 bytes, exceeds max_bytes=4096"
    ):
        validate_params(_rules_cmd(), {"rules": rules})


def test_validate_params_byte_cap_reads_a_string_as_utf8_not_escaped() -> None:
    """Shell text is capped on the bytes that ride, so an accent costs two, not six."""
    cmd = _cmd_with_params(
        apply=ParamDef(placeholder="apply", default=None, max_bytes=8)
    )
    assert validate_params(cmd, {"apply": "éééé"}) == {"apply": "éééé"}
    with pytest.raises(ParamValidationError, match="is 10 bytes, exceeds max_bytes=8"):
        validate_params(cmd, {"apply": "ééééé"})


def test_param_def_names_its_wire_type_from_the_schema() -> None:
    assert ParamDef(placeholder="p", default=None, pattern=".*").wire_type == "string"
    assert ParamDef(placeholder="p", default=None, max_bytes=8).wire_type == "string"
    assert _rules_cmd().params["rules"].wire_type == "array"
    shaped = ParamDef(
        placeholder="p", default=None, max_bytes=8, schema={"type": "object"}
    )
    assert shaped.wire_type == "object"


def test_param_def_default_must_be_a_value_of_the_declared_type() -> None:
    """A ``"{}"`` default on an object param would fail every dispatch that omits it."""
    with pytest.raises(ValueError, match="default .*expected array, got str"):
        _rules_param(default="[]")
    assert _rules_param(default=[]).default == []
    with pytest.raises(ValueError, match="default .*expected string, got int"):
        ParamDef(placeholder="tail", default=100, pattern="[0-9]+")
    with pytest.raises(ValueError, match="default .*missing required key"):
        _rules_param(default=[{"ID": "x"}])


def test_validate_params_secret_schema_failure_withholds_detail() -> None:
    """Neither the value nor the path into it rides the error for a secret blob."""
    cmd = _cmd_with_params(
        secret_blob=ParamDef(
            placeholder="secret_blob",
            default=None,
            max_bytes=4096,
            secret=True,
            schema={"type": "object", "keys": {"k": {"type": "string"}}},
        ),
    )
    with pytest.raises(ParamValidationError, match="withheld: secret") as excinfo:
        validate_params(cmd, {"secret_blob": {"LeakedKeyName": "LeakedValue"}})
    text = str(excinfo.value)
    assert "LeakedKeyName" not in text and "LeakedValue" not in text and "$" not in text


def test_pattern_less_schema_less_params_are_the_known_opaque_six() -> None:
    """Shell text and the rclone secrets are opaque by design. Every other param
    declares a pattern or a schema; a new opaque one must be added here by name."""
    opaque = {
        (command, param)
        for command, entry in wire_contract_commands().items()
        for param, validator in entry["params"].items()
        if validator["pattern"] is None and validator["schema"] is None
    }
    assert opaque == {
        ("run_verify_block", "verify_command"),
        ("run_apply_block", "apply_command"),
        ("rclone_estimate", "src_secret_access_key"),
        ("rclone_migrate", "src_secret_access_key"),
        ("rclone_migrate", "dst_secret_access_key"),
        ("rclone_restore_test", "dst_secret_access_key"),
    }


# -- secrets stay off event and log context ----------------------------------


def test_non_secret_params_drops_secret_flagged() -> None:
    # A secret param reaches the handler but must never ride event/log context.
    cmd = CommandSpec(
        group="rclone",
        command=["/rclone"],
        timeout=60,
        mode="job",
        handler=lambda p: None,
        params={
            "bucket_id": ParamDef("bucket_id", default=None, pattern=r".+"),
            "secret_access_key": ParamDef(
                "secret_access_key", default=None, pattern=r".+", secret=True
            ),
        },
    )
    full = {"bucket_id": "abc", "secret_access_key": "wJalrXUtnFEMI"}
    assert non_secret_params(cmd, full) == {"bucket_id": "abc"}


def test_non_secret_params_passes_all_when_none_secret() -> None:
    cmd = CommandSpec(
        group="deploy",
        command=["/bin/git", "pull"],
        timeout=60,
        params={"branch": ParamDef("branch", default=None, pattern=r".+")},
    )
    assert non_secret_params(cmd, {"branch": "main"}) == {"branch": "main"}


def test_credential_shaped_param_name_requires_secret_flag() -> None:
    # Fix-the-system guard for the events-plane leak: a future param named
    # like a credential cannot be constructed untagged.
    with pytest.raises(ValueError, match="secret=True"):
        ParamDef("api_token", default=None, pattern=r".+")
    with pytest.raises(ValueError, match="secret=True"):
        ParamDef("db_password", default=None, pattern=r".+")
    # Tagged, it constructs fine.
    pdef = ParamDef("api_token", default=None, pattern=r".+", secret=True)
    assert pdef.secret is True
