"""The Storm Pulse integration SDK (CORE-007 decision 5).

Foundation layer (CORE-000): this package imports **nothing** from the rest of
``stormpulse`` and carries no host-mutating primitive. It is the stable, versioned
contract a private integration's wizard is written against: ``stdlib`` plus this
package only. The Framework wizard engine (``stormpulse/wizard/``) consumes this
data and owns every side effect.

``SDK_API`` is the single integer version of this contract. A plan or manifest
built against a newer ``SDK_API`` than the running host is refused (I14); the
concrete ``stormpulse update`` behavior on a compat break is CORE-002 / P3 work.
"""

from __future__ import annotations

from stormpulse.sdk.declaration import (
    SdkCommandHandler,
    SdkCommandMode,
    SdkCommandSpec,
    SdkIntegration,
    SdkJobHandler,
    SdkJobOutcome,
    SdkParamDef,
    SdkProgress,
    command_specs_digest,
)
from stormpulse.sdk.findings import Finding, Severity, severity_rank
from stormpulse.sdk.plan import (
    CaddyDropIn,
    ClaimTomlSection,
    CreateSystemdUserUnit,
    InitContext,
    InitPlan,
    InstallBinary,
    InstallFile,
    Mutation,
    MutationKind,
    RestartOrReload,
    TomlScalar,
    VerifyProbe,
    mutation_kind,
)
from stormpulse.sdk.questions import (
    Answer,
    Answers,
    Question,
    QuestionKind,
    answers_from,
)
from stormpulse.sdk.readiness import (
    CAPABILITY_RE,
    Capability,
    CapabilityLiveness,
    CapabilityStatus,
    DependencyRequirement,
    ReadinessReport,
    ReadinessState,
    is_capability_token,
)
from stormpulse.sdk.wizard import IntegrationWizard

SDK_API: int = 1

__all__ = [
    "SDK_API",
    # declaration surface (CORE-007 external adapters)
    "SdkCommandHandler",
    "SdkCommandMode",
    "SdkCommandSpec",
    "SdkIntegration",
    "SdkJobHandler",
    "SdkJobOutcome",
    "SdkParamDef",
    "SdkProgress",
    "command_specs_digest",
    # findings
    "Finding",
    "Severity",
    "severity_rank",
    # questions
    "Answer",
    "Answers",
    "Question",
    "QuestionKind",
    "answers_from",
    # readiness
    "CAPABILITY_RE",
    "Capability",
    "CapabilityLiveness",
    "CapabilityStatus",
    "DependencyRequirement",
    "ReadinessReport",
    "ReadinessState",
    "is_capability_token",
    # plan
    "CaddyDropIn",
    "ClaimTomlSection",
    "CreateSystemdUserUnit",
    "InitContext",
    "InitPlan",
    "InstallBinary",
    "InstallFile",
    "Mutation",
    "MutationKind",
    "RestartOrReload",
    "TomlScalar",
    "VerifyProbe",
    "mutation_kind",
    # wizard
    "IntegrationWizard",
]
