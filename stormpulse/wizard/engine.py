"""The wizard mutation engine: preview, apply, verify, receipt, rollback (P2).

Framework layer. Applies an ``InitPlan``'s mutations in order; each step captures
its pre-image before mutating (I3). Any forward failure or failed verify triggers
a reverse-order rollback (I4); a compensation that itself fails is recorded, turns
the status into ``partial_rollback``, and is surfaced loudly, never silently
skipped (I5). The engine claims no cross-kind atomicity (I6): best-effort steps
are labeled in the preview.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone

from stormpulse.sdk import SDK_API, InitPlan, Mutation, mutation_kind
from stormpulse.wizard.env import ApplyEnv
from stormpulse.wizard.errors import WizardError
from stormpulse.wizard.journal import Journal, wizard_lock
from stormpulse.wizard.mutations import Step, build_step
from stormpulse.wizard.receipt import (
    STATUS_COMMITTED,
    STATUS_PARTIAL_ROLLBACK,
    STATUS_ROLLED_BACK,
    AppliedMutation,
    MutationReceipt,
    applied,
    persist_receipt,
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


def _post_apply_checks(env: ApplyEnv) -> list[str]:
    """The normative post-apply checks (§12.3): re-parse the config with no host
    probe, then run the caller-injected health + dependency re-check. A non-empty
    result rolls the plan back."""
    failures: list[str] = []
    if env.config_path.is_file():
        try:
            tomllib.loads(env.config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            failures.append(f"config no longer parses after apply: {exc}")
    if env.post_check is not None:
        failures.extend(env.post_check())
    return failures


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
    """Apply a plan transactionally under the wizard-apply lock. Every mutation is
    journaled and fsynced BEFORE its forward op, so a crash mid-apply leaves a
    durable record ``doctor`` can report and recover from. Returns a receipt; raises
    ``WizardError`` only for a pre-apply refusal (SDK-version mismatch or empty
    plan), before any lock, journal, or host change."""
    if plan.sdk_api > SDK_API:
        raise WizardError(
            f"plan targets SDK {plan.sdk_api} but this host offers SDK {SDK_API} "
            "(not offered by this Pulse version)"
        )
    if not plan.mutations:
        raise WizardError("plan has no mutations")

    done: list[_Done] = []
    applied_at = datetime.now(timezone.utc).isoformat()

    def rolled_back_receipt(failure: str) -> MutationReceipt:
        status, records, comp_failures = _rollback(done)
        full_failure = "; ".join([failure, *comp_failures])
        return MutationReceipt(
            agent_id=agent_id,
            integration_id=plan.integration_id,
            sdk_api=plan.sdk_api,
            plan_summary=plan.summary,
            status=status,
            applied_at=applied_at,
            applied=records,
            failure=full_failure,
        )

    journal = Journal(env.state_dir)
    with wizard_lock(env.state_dir):
        journal.begin(
            agent_id=agent_id,
            integration_id=plan.integration_id,
            sdk_api=plan.sdk_api,
            summary=plan.summary,
        )
        receipt: MutationReceipt | None = None
        for index, mutation in enumerate(plan.mutations):
            try:
                step = build_step(mutation, env)
            except WizardError as exc:
                receipt = rolled_back_receipt(f"build failed: {exc}")
                break
            # Durable-before-forward: the journal entry (with the captured
            # pre-image) is fsynced here, before the host is touched.
            journal.record(
                index=index,
                kind=step.kind.value,
                target=step.target,
                recover_path=step.recover_path,
                pre_image=step.pre_image,
                recover_mode=step.recover_mode,
            )
            entry = _Done(step=step, verified=False)
            done.append(entry)
            try:
                step.forward()
            except Exception as exc:  # noqa: BLE001 - any forward failure rolls the plan back
                receipt = rolled_back_receipt(f"forward failed on {step.kind.value}: {exc}")
                break
            entry.verified = step.verify()
            if not entry.verified:
                receipt = rolled_back_receipt(f"verify failed on {step.kind.value}")
                break

        if receipt is None:
            # All mutations applied and verified; run the normative post-apply
            # checks before committing. A failure here rolls the whole plan back.
            post_failures = _post_apply_checks(env)
            if post_failures:
                receipt = rolled_back_receipt(
                    "post-apply checks failed: " + "; ".join(post_failures)
                )
            else:
                records = tuple(
                    applied(
                        entry.step.kind,
                        entry.step.target,
                        pre_image_digest=entry.step.pre_image_digest,
                        verified=entry.verified,
                    )
                    for entry in done
                )
                receipt = MutationReceipt(
                    agent_id=agent_id,
                    integration_id=plan.integration_id,
                    sdk_api=plan.sdk_api,
                    plan_summary=plan.summary,
                    status=STATUS_COMMITTED,
                    applied_at=applied_at,
                    applied=records,
                )

        # A committed or fully-rolled-back apply is consistent: drop the journal.
        # A partial_rollback left inconsistent host state, so keep the journal so a
        # later ``doctor`` can recover the file-based steps out-of-process.
        if receipt.status != STATUS_PARTIAL_ROLLBACK:
            journal.finalize()
        # Persist the outcome record atomically (audit trail; not a load grant).
        persist_receipt(env.state_dir, receipt)
        return receipt
