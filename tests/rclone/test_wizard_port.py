"""C10: the rclone SDK port produces byte-identical config to the legacy path, and
adds a rollback the legacy path never had."""

from __future__ import annotations

from pathlib import Path

from stormpulse.rclone.init import append_rclone_section
from stormpulse.rclone.wizard import RCLONE_WIZARD
from stormpulse.sdk import SDK_API, Answer, InitContext, Severity, answers_from
from stormpulse.wizard import STATUS_COMMITTED, STATUS_ROLLED_BACK, ApplyEnv, apply_plan

_BASE = '[core]\nagent_id = "x"\n'
_BINARY = "/usr/bin/rclone"


def _answers() -> dict[str, Answer]:
    return answers_from([Answer("binary_path", _BINARY), Answer("as_runner", "yes")])


def _context(cfg: Path) -> InitContext:
    return InitContext(mode="user", config_path=str(cfg), discovered={"binary_path": _BINARY})


def test_wizard_plan_shape() -> None:
    ctx = _context(Path("/tmp/x"))
    plan = RCLONE_WIZARD.plan(_answers(), ctx)
    assert plan.sdk_api == SDK_API
    assert plan.integration_id == "rclone"
    kinds = [type(m).__name__ for m in plan.mutations]
    assert kinds == ["ClaimTomlSection", "RestartOrReload"]


def test_inspect_refuses_relative_path() -> None:
    ctx = _context(Path("/tmp/x"))
    answers = answers_from([Answer("binary_path", "rclone"), Answer("as_runner", "yes")])
    findings = RCLONE_WIZARD.inspect(answers, ctx)
    assert any(f.severity is Severity.REFUSAL for f in findings)


def test_sdk_config_is_byte_identical_to_legacy(tmp_path: Path) -> None:
    # Legacy path: append_rclone_section writes the [rclone] block.
    legacy = tmp_path / "legacy.toml"
    legacy.write_text(_BASE, encoding="utf-8")
    append_rclone_section(legacy, binary_path=_BINARY)

    # SDK path: apply the wizard plan through the engine (restart is a no-op that
    # does not touch the file; health True so it commits).
    sdk = tmp_path / "sdk.toml"
    sdk.write_text(_BASE, encoding="utf-8")
    env = ApplyEnv(
        config_path=sdk,
        base_dir=tmp_path / "base",
        systemd_user_dir=tmp_path / "units",
        restart=lambda unit, action: None,
        health=lambda unit: True,
    )
    plan = RCLONE_WIZARD.plan(_answers(), _context(sdk))
    receipt = apply_plan(plan, env, agent_id="agent-x")
    assert receipt.status == STATUS_COMMITTED
    assert sdk.read_bytes() == legacy.read_bytes()  # C10: byte-identical


def test_sdk_path_rolls_back_on_restart_failure(tmp_path: Path) -> None:
    # The rollback the legacy path never had: a failed restart removes the section.
    cfg = tmp_path / "s.toml"
    cfg.write_text(_BASE, encoding="utf-8")
    original = cfg.read_bytes()
    env = ApplyEnv(
        config_path=cfg,
        base_dir=tmp_path / "base",
        systemd_user_dir=tmp_path / "units",
        restart=lambda unit, action: None,
        health=lambda unit: False,  # restart verify fails -> rollback
    )
    plan = RCLONE_WIZARD.plan(_answers(), _context(cfg))
    receipt = apply_plan(plan, env, agent_id="a")
    assert receipt.status == STATUS_ROLLED_BACK
    assert cfg.read_bytes() == original  # [rclone] section rolled back
