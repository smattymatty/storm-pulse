"""SDK data-type behavior: question validation, capability tokens, readiness
ordering, mutation kinds, and finding order."""

from __future__ import annotations

import pytest

from stormpulse.sdk import (
    SDK_API,
    Answer,
    CapabilityLiveness,
    CapabilityStatus,
    ClaimTomlSection,
    DependencyRequirement,
    Finding,
    InitPlan,
    Question,
    QuestionKind,
    ReadinessReport,
    ReadinessState,
    RestartOrReload,
    Severity,
    VerifyProbe,
    answers_from,
    is_capability_token,
    mutation_kind,
    severity_rank,
)
from stormpulse.sdk.plan import MutationKind


def test_sdk_api_is_one() -> None:
    assert SDK_API == 1


@pytest.mark.parametrize(
    "token,ok",
    [
        ("garage.admin.v1", True),
        ("caddy.drop_in.v1", True),
        ("pulse.integration.commands.v1", True),
        ("pulse.integration.state.v1", True),
        ("nodots", False),
        ("Garage.admin.v1", False),
        ("garage.admin", False),
        ("garage.admin.vx", False),
    ],
)
def test_capability_token_regex(token: str, ok: bool) -> None:
    assert is_capability_token(token) is ok


def test_choice_question_requires_choices() -> None:
    bad = Question(key="k", kind=QuestionKind.CHOICE, prompt="pick")
    assert bad.validate() is not None
    good = Question(key="k", kind=QuestionKind.CHOICE, prompt="pick", choices=("a", "b"))
    assert good.validate() is None


def test_non_choice_question_must_not_carry_choices() -> None:
    bad = Question(key="k", kind=QuestionKind.TEXT, prompt="p", choices=("a",))
    assert bad.validate() is not None


def test_bounds_only_on_numeric_kinds_and_ordered() -> None:
    assert Question(key="k", kind=QuestionKind.TEXT, prompt="p", min=1).validate() is not None
    assert Question(key="k", kind=QuestionKind.PORT, prompt="p", min=1, max=65535).validate() is None
    assert Question(key="k", kind=QuestionKind.INTEGER, prompt="p", min=5, max=1).validate() is not None


def test_answers_from_rejects_duplicate_keys() -> None:
    with pytest.raises(ValueError):
        answers_from([Answer("k", "1"), Answer("k", "2")])
    got = answers_from([Answer("a", "1"), Answer("b", "2")])
    assert set(got) == {"a", "b"}


def test_readiness_state_is_ordered() -> None:
    assert ReadinessState.AVAILABLE < ReadinessState.CONFIGURED
    assert ReadinessState.CONFIGURED < ReadinessState.ENABLED
    assert ReadinessState.ENABLED < ReadinessState.READY
    assert ReadinessState.READY >= ReadinessState.ENABLED


def test_readiness_report_baseline_live() -> None:
    ready = ReadinessReport("garage", ReadinessState.READY)
    assert ready.baseline_live is True
    enabled = ReadinessReport("garage", ReadinessState.ENABLED)
    assert enabled.baseline_live is False


def test_dependency_requirement_defaults_to_ready() -> None:
    req = DependencyRequirement(id="garage")
    assert req.state is ReadinessState.READY
    assert req.capability is None


def test_capability_status_fields() -> None:
    cs = CapabilityStatus("garage.admin.v1", CapabilityLiveness.UNMET, reason="no token", repair="fix it")
    assert cs.liveness is CapabilityLiveness.UNMET
    assert cs.repair == "fix it"


def test_mutation_kind_reads_classvar() -> None:
    assert mutation_kind(ClaimTomlSection("rclone", {"enabled": True})) is MutationKind.CLAIM_TOML_SECTION
    assert mutation_kind(VerifyProbe("garage.admin.v1")) is MutationKind.VERIFY_PROBE
    assert mutation_kind(RestartOrReload("stormpulse")) is MutationKind.RESTART_OR_RELOAD


def test_init_plan_carries_sdk_api() -> None:
    plan = InitPlan(
        sdk_api=SDK_API,
        integration_id="rclone",
        mutations=(ClaimTomlSection("rclone", {"enabled": True}),),
        summary="configure rclone",
    )
    assert plan.sdk_api == 1
    first = plan.mutations[0]
    assert isinstance(first, ClaimTomlSection)
    assert first.section == "rclone"


def test_finding_severity_order() -> None:
    findings = [
        Finding(Severity.OK, "ok"),
        Finding(Severity.REFUSAL, "no"),
        Finding(Severity.WARNING, "careful"),
    ]
    ordered = sorted(findings, key=lambda f: (severity_rank(f.severity), f.message))
    assert [f.severity for f in ordered] == [Severity.REFUSAL, Severity.WARNING, Severity.OK]
