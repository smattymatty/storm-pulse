"""The Storm Pulse wizard engine (CORE-007 decision 5).

Framework layer (CORE-000): imports Foundation (``sdk``, ``config``) only, never a
Feature. It consumes the SDK's typed ``InitPlan`` and owns every host side effect:
preview, ordered apply, per-step verify, receipt, and reverse-order compensating
rollback. A private integration's wizard produces the plan; this engine applies it.
"""

from __future__ import annotations

from stormpulse.wizard.engine import (
    PlanPreview,
    PreviewStep,
    apply_plan,
    preview_plan,
)
from stormpulse.wizard.env import ApplyEnv, CapabilityProvider
from stormpulse.wizard.errors import CompensationError, WizardError
from stormpulse.wizard.journal import (
    JournalEntry,
    RecoveryResult,
    read_pending,
    recover,
)
from stormpulse.wizard.providers import (
    get_provider,
    register_capability_provider,
    registered_providers,
)
from stormpulse.wizard.receipt import (
    STATUS_COMMITTED,
    STATUS_PARTIAL_ROLLBACK,
    STATUS_ROLLED_BACK,
    AppliedMutation,
    MutationReceipt,
)

__all__ = [
    "ApplyEnv",
    "AppliedMutation",
    "CapabilityProvider",
    "CompensationError",
    "JournalEntry",
    "MutationReceipt",
    "PlanPreview",
    "PreviewStep",
    "RecoveryResult",
    "STATUS_COMMITTED",
    "STATUS_PARTIAL_ROLLBACK",
    "STATUS_ROLLED_BACK",
    "WizardError",
    "apply_plan",
    "get_provider",
    "preview_plan",
    "read_pending",
    "recover",
    "register_capability_provider",
    "registered_providers",
]
