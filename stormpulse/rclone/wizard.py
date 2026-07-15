"""The rclone setup, ported to the wizard SDK (P2 deliverable #4, CORE-007).

This is the proof that the SDK is not a third-party-only abstraction: a built-in
setup expressed as an ``IntegrationWizard`` that returns typed data, driven by the
core engine. The legacy procedural ``run_rclone_init`` stays working (additive);
this path additionally gives rclone a rollback the legacy path never had. The
produced ``[rclone]`` section is byte-identical to the legacy template (C10).

Feature layer: imports Foundation (``sdk``) and its own package only.
"""

from __future__ import annotations

from stormpulse.rclone.init import find_rclone_binary
from stormpulse.sdk import (
    SDK_API,
    Answers,
    ClaimTomlSection,
    Finding,
    InitContext,
    InitPlan,
    Question,
    QuestionKind,
    RestartOrReload,
    Severity,
)

_TRUTHY = {"yes", "y", "true", "1"}


class RcloneWizard:
    """Configure the box as a backup Runner: detect rclone, claim ``[rclone]``,
    restart. An ``IntegrationWizard`` (structural)."""

    def questions(self, context: InitContext) -> list[Question]:
        discovered = context.discovered.get("binary_path") or find_rclone_binary()
        return [
            Question(
                key="binary_path",
                kind=QuestionKind.PATH,
                prompt="rclone binary path",
                default=discovered,
            ),
            Question(
                key="as_runner",
                kind=QuestionKind.CONFIRM,
                prompt="Configure this box as a backup Runner?",
            ),
        ]

    def inspect(self, answers: Answers, context: InitContext) -> list[Finding]:
        findings: list[Finding] = []
        if answers.get("as_runner") is not None and answers["as_runner"].value.lower() not in _TRUTHY:
            findings.append(Finding(Severity.REFUSAL, "Runner setup declined."))
            return findings
        binary_path = answers["binary_path"].value
        if not binary_path.startswith("/"):
            findings.append(
                Finding(
                    Severity.REFUSAL,
                    f"binary path must be absolute, got {binary_path!r}",
                )
            )
        elif find_rclone_binary(binary_path) is None:
            findings.append(
                Finding(
                    Severity.REFUSAL,
                    f"no working rclone at {binary_path}",
                    repair="install rclone (https://rclone.org/install)",
                )
            )
        else:
            findings.append(Finding(Severity.OK, f"rclone works at {binary_path}"))
        return findings

    def plan(self, answers: Answers, context: InitContext) -> InitPlan:
        binary_path = answers["binary_path"].value
        return InitPlan(
            sdk_api=SDK_API,
            integration_id="rclone",
            mutations=(
                ClaimTomlSection(
                    "rclone", {"enabled": True, "binary_path": binary_path}
                ),
                RestartOrReload("stormpulse"),
            ),
            summary="Configure this box as a backup Runner (rclone)",
        )


RCLONE_WIZARD = RcloneWizard()
