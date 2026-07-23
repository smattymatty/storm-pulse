"""Drive an ``IntegrationWizard`` through the host wizard engine (CORE-007 D5):
questions -> collect answers -> inspect (refusals block) -> plan -> preview ->
confirm -> transactional apply with rollback and a durable journal.

The single interactive runner behind both `stormpulse rclone init` and
`stormpulse integration init <id>`, so a built-in and an external adapter get the
exact same setup quality from one code path.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from stormpulse.sdk import Answer, InitContext, QuestionKind, Severity
from stormpulse.sdk.wizard import IntegrationWizard


def drive_wizard(
    wizard: IntegrationWizard,
    context: InitContext,
    *,
    config: Any,
    config_path: Path,
    mode: Any,
    label: str,
) -> bool:
    """Run the wizard end to end. Returns True if a plan committed, False if the
    operator aborted at the confirm; exits the process on a refusal or a failed
    apply. ``config`` is the loaded Config (for state dir + agent id); ``mode`` is
    the InstallMode (for the restart hint)."""
    from stormpulse.init.prompts import prompt, prompt_confirm
    from stormpulse.init.system import restart_or_hint
    from stormpulse.wizard import STATUS_COMMITTED, ApplyEnv, apply_plan, preview_plan

    answers: dict[str, Answer] = {}
    for question in wizard.questions(context):
        if question.kind is QuestionKind.DISCOVERED:
            continue
        if question.kind is QuestionKind.CONFIRM:
            value = "yes" if prompt_confirm(question.prompt) else "no"
        else:
            value = prompt(question.prompt, default=question.default or "")
        answers[question.key] = Answer(question.key, value)

    findings = wizard.inspect(answers, context)
    for finding in findings:
        repair = f" ({finding.repair})" if finding.repair else ""
        print(f"  [{finding.severity.value}] {finding.message}{repair}", file=sys.stderr)
    if any(f.severity is Severity.REFUSAL for f in findings):
        print("Aborted.", file=sys.stderr)
        sys.exit(1)

    plan = wizard.plan(answers, context)
    preview = preview_plan(plan)
    print("\nPlan:", file=sys.stderr)
    for step in preview.steps:
        tag = " (best-effort)" if step.best_effort else ""
        print(f"  {step.kind} {step.target}{tag}", file=sys.stderr)
    if not prompt_confirm("Apply this plan?"):
        print("Aborted.", file=sys.stderr)
        return False

    def restart(_unit: str, _action: str) -> None:
        restart_or_hint(mode)

    env = ApplyEnv(
        config_path=config_path,
        base_dir=config_path.parent,
        systemd_user_dir=config_path.parent,
        state_dir=config.storage.db_path.parent,
        restart=restart,
    )
    receipt = apply_plan(plan, env, agent_id=config.agent.id)
    if receipt.status == STATUS_COMMITTED:
        print(f"\n  [{label}] configured; {len(receipt.applied)} step(s) applied.", file=sys.stderr)
        return True
    print(f"apply {receipt.status}: {receipt.failure}", file=sys.stderr)
    sys.exit(5)
