"""The ``IntegrationWizard`` protocol (CORE-007 decision 5).

Foundation layer. An integration author implements this against ``stdlib`` and
``stormpulse.sdk`` only. Every method is pure: it reads the read-only
``InitContext`` and typed ``Answers`` and returns SDK data. The host owns
rendering, validation, preview, application, verification, and rollback (I2).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from stormpulse.sdk.findings import Finding
from stormpulse.sdk.plan import InitContext, InitPlan
from stormpulse.sdk.questions import Answers, Question


@runtime_checkable
class IntegrationWizard(Protocol):
    """A wizard: declare questions, inspect answers into findings, and build a plan.

    Implementations hold no host handle. A ``refusal`` finding from ``inspect``
    blocks the plan; ``plan`` is only reached once the host has rendered the
    questions, collected answers, and cleared inspection.
    """

    def questions(self, context: InitContext) -> list[Question]:
        """The questions to ask, given the read-only context (discovered values,
        dependency readiness, mode)."""
        ...

    def inspect(self, answers: Answers, context: InitContext) -> list[Finding]:
        """Findings for the given answers. A ``refusal`` blocks application."""
        ...

    def plan(self, answers: Answers, context: InitContext) -> InitPlan:
        """The ordered mutation plan the host will preview, apply, and (on failure)
        roll back."""
        ...
