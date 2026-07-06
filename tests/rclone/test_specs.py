"""Contract tests for the rclone Integration: spec shape, group discipline,
and the secret-redaction proof (event context must carry no secret)."""

from __future__ import annotations

import pytest

import stormpulse.rclone.integration as rclone_integration
from stormpulse.commands.registry import (
    ParamValidationError,
    non_secret_params,
    validate_params,
)
from stormpulse.integrations import registered_integrations
from stormpulse.rclone.commands import build_rclone_specs
from tests.rclone.helpers import CONFIG, DST_PARAMS, SRC_PARAMS


def test_rclone_is_registered_stateless() -> None:
    integration = next(
        i for i in registered_integrations() if i.id == rclone_integration.RCLONE_INTEGRATION.id
    )
    assert integration.specs is not None
    assert integration.preconditions is not None
    # Stateless by design: no state surface, no merge involvement.
    assert integration.discover is None
    assert integration.collect_state is None
    assert integration.detect is None
    assert integration.read_affected is None
    assert integration.log_enrichers is None


def test_three_jobs_all_grouped_rclone() -> None:
    specs = build_rclone_specs(CONFIG)
    assert set(specs) == {"rclone_estimate", "rclone_migrate", "rclone_restore_test"}
    for spec in specs.values():
        assert spec.group == "rclone"
        assert spec.mode == "job"
        assert spec.handler is not None
    assert specs["rclone_estimate"].read_only is True
    assert specs["rclone_restore_test"].read_only is False
    assert specs["rclone_migrate"].sensitive_output is True


def test_secret_params_never_reach_event_context() -> None:
    # The redaction proof: dispatch builds event context via
    # non_secret_params; no secret value may survive it, on any job.
    specs = build_rclone_specs(CONFIG)
    full = {**SRC_PARAMS, **DST_PARAMS}
    for name, spec in specs.items():
        params = {k: v for k, v in full.items() if k in spec.params}
        validated = validate_params(spec, params)
        context = non_secret_params(spec, validated)
        assert "src_secret_access_key" not in context, name
        assert "dst_secret_access_key" not in context, name
        for value in context.values():
            assert "secret" not in value.lower(), name


def test_handlers_build_from_valid_params() -> None:
    specs = build_rclone_specs(CONFIG)
    full = {**SRC_PARAMS, **DST_PARAMS}
    for name, spec in specs.items():
        params = {k: v for k, v in full.items() if k in spec.params}
        assert spec.handler is not None
        assert spec.handler(params) is not None, name


def test_plaintext_endpoints_are_rejected() -> None:
    # https only: a plaintext endpoint would move customer objects
    # unencrypted, so http must fail validation, not silently pass.
    specs = build_rclone_specs(CONFIG)
    params = {**SRC_PARAMS, "src_endpoint": "http://s3.source.example"}
    with pytest.raises(ParamValidationError):
        validate_params(specs["rclone_estimate"], params)


def test_handlers_refuse_missing_credentials() -> None:
    specs = build_rclone_specs(CONFIG)
    incomplete = {k: v for k, v in SRC_PARAMS.items() if k != "src_secret_access_key"}
    handler_thunk = specs["rclone_estimate"].handler
    assert handler_thunk is not None
    assert handler_thunk(incomplete) is None
