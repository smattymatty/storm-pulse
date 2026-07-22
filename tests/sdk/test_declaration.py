"""The CORE-007 external-adapter declaration surface: construction guards and the
canonical command_specs_digest (the hash a control-plane allow rule binds to, so
its stability and its sensitivity both matter)."""

from __future__ import annotations

import pytest

from stormpulse.sdk import (
    SdkCommandSpec,
    SdkIntegration,
    SdkJobOutcome,
    SdkParamDef,
    SdkProgress,
    command_specs_digest,
)


async def _noop(_progress: SdkProgress) -> SdkJobOutcome:  # a stand-in job body
    return SdkJobOutcome(success=True)


def _job(name: str, *, timeout: int = 30, params: dict[str, SdkParamDef] | None = None) -> SdkCommandSpec:
    return SdkCommandSpec(
        group="buckets_gate",
        command=[name],
        timeout=timeout,
        mode="job",
        handler=lambda _params: _noop,
        params=params or {},
    )


# ---------------------------------------------------------------------------
# Construction guards mirror the internal contract
# ---------------------------------------------------------------------------


def test_param_requires_a_validator() -> None:
    with pytest.raises(ValueError):
        SdkParamDef(placeholder="x", default=None)


def test_credential_shaped_name_requires_secret() -> None:
    with pytest.raises(ValueError):
        SdkParamDef(placeholder="api_token", default=None, max_bytes=16)
    # secret=True lifts the guard
    SdkParamDef(placeholder="api_token", default=None, max_bytes=16, secret=True)


def test_job_requires_handler() -> None:
    with pytest.raises(ValueError):
        SdkCommandSpec(group="g", command=["j"], timeout=5, mode="job")


def test_non_job_must_not_carry_handler() -> None:
    with pytest.raises(ValueError):
        SdkCommandSpec(
            group="g", command=["/usr/bin/true"], timeout=5, mode="subprocess",
            handler=lambda _p: _noop,
        )


def test_subprocess_needs_absolute_argv0() -> None:
    with pytest.raises(ValueError):
        SdkCommandSpec(group="g", command=["true"], timeout=5, mode="subprocess")


# ---------------------------------------------------------------------------
# command_specs_digest: stable, order-independent, semantically sensitive
# ---------------------------------------------------------------------------


def test_digest_is_deterministic_and_order_independent() -> None:
    a = {"one": _job("one"), "two": _job("two")}
    b = {"two": _job("two"), "one": _job("one")}  # insertion order flipped
    assert command_specs_digest(a) == command_specs_digest(b)
    assert command_specs_digest(a).startswith("sha256:")


def test_digest_ignores_handler_identity_and_description() -> None:
    base = _job("one")
    other_handler = SdkCommandSpec(
        group="buckets_gate", command=["one"], timeout=30, mode="job",
        handler=lambda _p: _noop, description="a different description",
    )
    assert command_specs_digest({"one": base}) == command_specs_digest({"one": other_handler})


def test_digest_is_sensitive_to_semantic_change() -> None:
    base = command_specs_digest({"one": _job("one", timeout=30)})
    assert base != command_specs_digest({"one": _job("one", timeout=31)})
    assert base != command_specs_digest(
        {"one": _job("one", params={"p": SdkParamDef(placeholder="p", default=None, max_bytes=10)})}
    )


def test_integration_core_is_declarable() -> None:
    integ = SdkIntegration(
        id="buckets_gate",
        parse_config=lambda section: section,
        enabled=lambda cfg: True,
        specs=lambda cfg: {"buckets_gate_apply_policy": _job("buckets_gate_apply_policy")},
    )
    assert integ.id == "buckets_gate"
    assert integ.specs is not None
