"""I2/C6 (honest form): the wizard contract hands out no host handle.

CORE-007 is explicit that this is NOT a sandbox - in-process Python is trusted,
not confined. So the property proven here is the *contract*: a wizard's methods
receive a frozen, data-only ``InitContext`` and return SDK data; applying is the
host's job. A well-behaved wizard cannot accidentally mutate the host, and the
plan is inert until ``apply_plan`` runs it. Confinement of malicious code is P3+
out-of-process work, deliberately not claimed here.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from stormpulse.rclone.wizard import RCLONE_WIZARD
from stormpulse.sdk import (
    Answer,
    InitContext,
    IntegrationWizard,
    answers_from,
)


def test_init_context_is_frozen_dataclass() -> None:
    ctx = InitContext(mode="user", config_path="/tmp/x")
    assert dataclasses.is_dataclass(ctx)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.mode = "system"  # type: ignore[misc]


def test_init_context_exposes_only_data_fields() -> None:
    fields = {f.name for f in dataclasses.fields(InitContext)}
    assert fields == {"mode", "config_path", "discovered", "dependencies"}


def test_rclone_wizard_conforms_to_protocol() -> None:
    assert isinstance(RCLONE_WIZARD, IntegrationWizard)


def test_wizard_methods_are_side_effect_free(tmp_path: Path) -> None:
    cfg = tmp_path / "stormpulse.toml"
    cfg.write_text('[core]\nagent_id = "x"\n', encoding="utf-8")
    before = sorted(p.name for p in tmp_path.iterdir())
    ctx = InitContext(mode="user", config_path=str(cfg), discovered={"binary_path": "/usr/bin/rclone"})
    answers = answers_from([Answer("binary_path", "/usr/bin/rclone"), Answer("as_runner", "yes")])

    RCLONE_WIZARD.questions(ctx)
    RCLONE_WIZARD.inspect(answers, ctx)
    plan = RCLONE_WIZARD.plan(answers, ctx)

    # Building the plan changed nothing on disk; the plan is inert data.
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert cfg.read_text(encoding="utf-8") == '[core]\nagent_id = "x"\n'
    assert plan.mutations  # it IS a plan, just not applied
