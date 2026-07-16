"""CLI handler for ``stormpulse rclone`` subcommand group."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from stormpulse.init.files import default_config_path

logger = logging.getLogger("stormpulse")

_DEFAULT_CONFIG = default_config_path()


def cmd_rclone_init(args: argparse.Namespace) -> None:
    from stormpulse.init import InitError
    from stormpulse.rclone.init import run_rclone_init

    if getattr(args, "sdk", False):
        _run_rclone_sdk_init(
            Path(args.config), binary_path_override=args.binary_path, force=args.force
        )
        return
    try:
        run_rclone_init(
            Path(args.config),
            binary_path_override=args.binary_path,
            force=args.force,
        )
    except InitError as exc:
        logger.error("%s", exc)
        sys.exit(1)


def _run_rclone_sdk_init(
    config_path: Path, *, binary_path_override: str | None, force: bool
) -> None:
    """The rclone setup through the wizard SDK + engine (P2): wizard questions ->
    inspect -> plan preview -> confirm -> transactional apply with rollback and a
    durable journal. The legacy ``run_rclone_init`` path is untouched."""
    from stormpulse.config import load_config
    from stormpulse.init.mode import detect_mode
    from stormpulse.init.prompts import prompt, prompt_confirm
    from stormpulse.init.system import restart_or_hint
    from stormpulse.rclone.init import find_rclone_binary, has_rclone_section
    from stormpulse.rclone.wizard import RCLONE_WIZARD
    from stormpulse.sdk import Answer, InitContext, QuestionKind, Severity
    from stormpulse.wizard import STATUS_COMMITTED, ApplyEnv, apply_plan, preview_plan

    config = load_config(config_path)
    if has_rclone_section(config_path) and not force:
        logger.error("[rclone] section already exists in %s; use --force", config_path)
        sys.exit(1)

    mode = detect_mode()
    discovered = find_rclone_binary(binary_path_override)
    context = InitContext(
        mode=mode.name.lower(),
        config_path=str(config_path),
        discovered={"binary_path": discovered} if discovered else {},
    )

    answers: dict[str, Answer] = {}
    for question in RCLONE_WIZARD.questions(context):
        if question.kind is QuestionKind.DISCOVERED:
            continue
        if question.kind is QuestionKind.CONFIRM:
            value = "yes" if prompt_confirm(question.prompt) else "no"
        else:
            value = prompt(question.prompt, default=question.default or "")
        answers[question.key] = Answer(question.key, value)

    findings = RCLONE_WIZARD.inspect(answers, context)
    for finding in findings:
        repair = f" ({finding.repair})" if finding.repair else ""
        print(f"  [{finding.severity.value}] {finding.message}{repair}", file=sys.stderr)
    if any(f.severity is Severity.REFUSAL for f in findings):
        print("Aborted.", file=sys.stderr)
        sys.exit(1)

    plan = RCLONE_WIZARD.plan(answers, context)
    preview = preview_plan(plan)
    print("\nPlan:", file=sys.stderr)
    for step in preview.steps:
        tag = " (best-effort)" if step.best_effort else ""
        print(f"  {step.kind} {step.target}{tag}", file=sys.stderr)
    if not prompt_confirm("Apply this plan?"):
        print("Aborted.", file=sys.stderr)
        return

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
        print(
            f"\n  [rclone] configured; {len(receipt.applied)} step(s) applied.",
            file=sys.stderr,
        )
    else:
        logger.error("apply %s: %s", receipt.status, receipt.failure)
        sys.exit(5)


def add_rclone_subparser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Add the ``rclone`` subcommand group with nested subcommands."""
    rclone_parser = subparsers.add_parser(
        "rclone",
        help="rclone migration/backup Runner integration",
    )
    rclone_sub = rclone_parser.add_subparsers(dest="rclone_command")

    # --- rclone init ---
    init_parser = rclone_sub.add_parser(
        "init",
        help="detect rclone and configure this box as a backup Runner",
    )
    init_parser.add_argument(
        "--config",
        default=_DEFAULT_CONFIG,
        help=f"path to stormpulse.toml (default: {_DEFAULT_CONFIG})",
    )
    init_parser.add_argument(
        "--binary-path",
        default=None,
        help="path to the rclone binary (overrides auto-detection)",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing [rclone] section",
    )
    init_parser.add_argument(
        "--sdk",
        action="store_true",
        help="use the wizard SDK path (preview + transactional apply with rollback)",
    )
