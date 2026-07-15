"""The wizard mutation engine: preview, apply, verify, receipt, rollback (P2).

Framework layer. Applies an ``InitPlan``'s mutations in order; each step captures
its pre-image before mutating (I3). Any forward failure or failed verify triggers
a reverse-order rollback (I4); a compensation that itself fails is recorded, turns
the status into ``partial_rollback``, and is surfaced loudly, never silently
skipped (I5). The engine claims no cross-kind atomicity (I6): best-effort steps
are labeled in the preview.
"""

from __future__ import annotations

from dataclasses import dataclass

from stormpulse.sdk import SDK_API, InitPlan, Mutation, mutation_kind
from stormpulse.wizard.env import ApplyEnv
from stormpulse.wizard.errors import WizardError
from stormpulse.wizard.mutations import Step, build_step
from stormpulse.wizard.receipt import (
    STATUS_COMMITTED,
    STATUS_PARTIAL_ROLLBACK,
    STATUS_ROLLED_BACK,
    AppliedMutation,
    MutationReceipt,
    applied,
)

# Best-effort kinds, for the preview's honesty label (I6). Kept in sync with the
# ``atomic`` flags the step builder sets.
_BEST_EFFORT = {"create_systemd_user_unit", "caddy_drop_in", "restart_or_reload"}


@dataclass(frozen=True, slots=True)
class PreviewStep:
    """One line of a plan preview: what will change and whether it is best-effort."""

    kind: str
    target: str
    best_effort: bool


@dataclass(frozen=True, slots=True)
class PlanPreview:
    """A side-effect-free description of a plan, for operator confirmation."""

    integration_id: str
    summary: str
    steps: tuple[PreviewStep, ...]


def _describe(mutation: Mutation) -> PreviewStep:
    kind = mutation_kind(mutation).value
    target = getattr(
        mutation,
        "section",
        getattr(
            mutation,
            "rel_target",
            getattr(
                mutation,
                "unit_name",
                getattr(
                    mutation,
                    "drop_in_name",
                    getattr(mutation, "unit", getattr(mutation, "capability", "")),
                ),
            ),
        ),
    )
    return PreviewStep(kind=kind, target=str(target), best_effort=kind in _BEST_EFFORT)


def preview_plan(plan: InitPlan) -> PlanPreview:
    """Describe a plan without touching the host."""
    return PlanPreview(
        integration_id=plan.integration_id,
        summary=plan.summary,
        steps=tuple(_describe(m) for m in plan.mutations),
    )


@dataclass(slots=True)
class _Done:
    step: Step
    verified: bool


def _rollback(done: list[_Done]) -> tuple[str, tuple[AppliedMutation, ...], list[str]]:
    """Compensate applied steps in reverse. A failed compensation is recorded and
    escalates the status to ``partial_rollback``; the loop does not abort (I5)."""
    status = STATUS_ROLLED_BACK
    compensated: dict[int, bool] = {}
    failures: list[str] = []
    for entry in reversed(done):
        try:
            entry.step.compensate()
            compensated[id(entry)] = True
        except Exception as exc:  # noqa: BLE001 - a failed compensation must be loud, not fatal
            compensated[id(entry)] = False
            status = STATUS_PARTIAL_ROLLBACK
            failures.append(f"compensation failed for {entry.step.kind.value}: {exc}")
    applied_records = tuple(
        applied(
            entry.step.kind,
            entry.step.target,
            pre_image_digest=entry.step.pre_image_digest,
            verified=entry.verified,
            compensated=compensated.get(id(entry), False),
        )
        for entry in done
    )
    return status, applied_records, failures


def apply_plan(plan: InitPlan, env: ApplyEnv, *, agent_id: str) -> MutationReceipt:
    """Apply a plan transactionally. Returns a receipt; raises ``WizardError`` only
    for a pre-apply refusal (an SDK-version mismatch or an empty plan), before any
    host change."""
    if plan.sdk_api > SDK_API:
        raise WizardError(
            f"plan targets SDK {plan.sdk_api} but this host offers SDK {SDK_API} "
            "(not offered by this Pulse version)"
        )
    if not plan.mutations:
        raise WizardError("plan has no mutations")

    done: list[_Done] = []

    def rolled_back_receipt(failure: str) -> MutationReceipt:
        status, records, comp_failures = _rollback(done)
        full_failure = "; ".join([failure, *comp_failures])
        return MutationReceipt(
            agent_id=agent_id,
            integration_id=plan.integration_id,
            sdk_api=plan.sdk_api,
            plan_summary=plan.summary,
            status=status,
            applied=records,
            failure=full_failure,
        )

    for mutation in plan.mutations:
        try:
            step = build_step(mutation, env)
        except WizardError as exc:
            return rolled_back_receipt(f"build failed: {exc}")
        entry = _Done(step=step, verified=False)
        done.append(entry)
        try:
            step.forward()
        except Exception as exc:  # noqa: BLE001 - any forward failure rolls the plan back
            return rolled_back_receipt(f"forward failed on {step.kind.value}: {exc}")
        entry.verified = step.verify()
        if not entry.verified:
            return rolled_back_receipt(f"verify failed on {step.kind.value}")

    records = tuple(
        applied(
            entry.step.kind,
            entry.step.target,
            pre_image_digest=entry.step.pre_image_digest,
            verified=entry.verified,
        )
        for entry in done
    )
    return MutationReceipt(
        agent_id=agent_id,
        integration_id=plan.integration_id,
        sdk_api=plan.sdk_api,
        plan_summary=plan.summary,
        status=STATUS_COMMITTED,
        applied=records,
    )
