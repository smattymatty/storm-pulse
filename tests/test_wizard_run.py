"""The shared wizard runner (`cli/wizard_run.drive_wizard`) that backs both
`rclone init` and `integration init <id>`: it drives a wizard's
questions -> inspect -> plan -> preview -> transactional apply, and a committed
plan actually mutates stormpulse.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

import stormpulse.init.prompts as prompts
from stormpulse.cli.wizard_run import drive_wizard
from stormpulse.sdk import (
    SDK_API,
    Answers,
    ClaimTomlSection,
    Finding,
    InitContext,
    InitPlan,
    Question,
    QuestionKind,
    Severity,
)


class _StubWizard:
    """A minimal IntegrationWizard: one text question -> a [stub] section."""

    def __init__(self, *, refuse: bool = False) -> None:
        self._refuse = refuse

    def questions(self, context: InitContext) -> list[Question]:
        return [Question("val", QuestionKind.TEXT, "Value?", default="hi")]

    def inspect(self, answers: Answers, context: InitContext) -> list[Finding]:
        if self._refuse:
            return [Finding(severity=Severity.REFUSAL, message="nope")]
        return []

    def plan(self, answers: Answers, context: InitContext) -> InitPlan:
        return InitPlan(
            sdk_api=SDK_API,
            integration_id="stub",
            mutations=(ClaimTomlSection("stub", {"enabled": True, "val": answers["val"].value}),),
        )


def _config(tmp_path: Path) -> object:
    state = tmp_path / "state"
    state.mkdir()
    return SimpleNamespace(
        storage=SimpleNamespace(db_path=state / "db.sqlite"),
        agent=SimpleNamespace(id="agent-1"),
    )


def test_committed_plan_writes_the_toml_section(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "stormpulse.toml"
    cfg.write_text("[agent]\nid = 'agent-1'\n")
    monkeypatch.setattr(prompts, "prompt", lambda _p, default="": default)
    monkeypatch.setattr(prompts, "prompt_confirm", lambda _p: True)

    ok = drive_wizard(
        _StubWizard(), InitContext(mode="user", config_path=str(cfg)),
        config=_config(tmp_path), config_path=cfg, mode="user", label="stub",
    )
    assert ok is True
    doc = tomllib.loads(cfg.read_text())
    assert doc["stub"]["enabled"] is True
    assert doc["stub"]["val"] == "hi"


def test_declining_the_confirm_applies_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "stormpulse.toml"
    cfg.write_text("[agent]\nid = 'agent-1'\n")
    monkeypatch.setattr(prompts, "prompt", lambda _p, default="": default)
    monkeypatch.setattr(prompts, "prompt_confirm", lambda _p: False)  # decline

    ok = drive_wizard(
        _StubWizard(), InitContext(mode="user", config_path=str(cfg)),
        config=_config(tmp_path), config_path=cfg, mode="user", label="stub",
    )
    assert ok is False
    assert "stub" not in tomllib.loads(cfg.read_text())


def test_refusal_aborts_before_apply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = tmp_path / "stormpulse.toml"
    cfg.write_text("[agent]\nid = 'agent-1'\n")
    monkeypatch.setattr(prompts, "prompt", lambda _p, default="": default)
    monkeypatch.setattr(prompts, "prompt_confirm", lambda _p: True)

    with pytest.raises(SystemExit) as exc:
        drive_wizard(
            _StubWizard(refuse=True), InitContext(mode="user", config_path=str(cfg)),
            config=_config(tmp_path), config_path=cfg, mode="user", label="stub",
        )
    assert exc.value.code == 1
    assert "stub" not in tomllib.loads(cfg.read_text())
