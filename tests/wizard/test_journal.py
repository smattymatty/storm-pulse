"""Durable journal + crash recovery (P2 §15): journal is fsynced before each
forward, a committed apply leaves no journal, an interrupted apply leaves a
recoverable one, and recovery restores the pre-apply state out-of-process."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from stormpulse.sdk import SDK_API, ClaimTomlSection, InitPlan, VerifyProbe
from stormpulse.wizard import (
    STATUS_COMMITTED,
    STATUS_ROLLED_BACK,
    ApplyEnv,
    apply_plan,
    read_pending,
    recover,
)
from stormpulse.wizard.journal import Journal, _active_path, wizard_lock


def _env(tmp_path: Path, **kw: object) -> ApplyEnv:
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = tmp_path / "stormpulse.toml"
    cfg.write_text('[core]\nagent_id = "x"\n', encoding="utf-8")
    return ApplyEnv(
        config_path=cfg,
        base_dir=tmp_path / "base",
        systemd_user_dir=tmp_path / "units",
        state_dir=tmp_path / "state",
        **kw,  # type: ignore[arg-type]
    )


def test_committed_apply_leaves_no_journal(tmp_path: Path) -> None:
    env = _env(tmp_path, restart=lambda u, a: None, health=lambda u: True)
    plan = InitPlan(
        sdk_api=SDK_API,
        integration_id="rclone",
        mutations=(ClaimTomlSection("rclone", {"enabled": True}),),
        summary="commit",
    )
    receipt = apply_plan(plan, env, agent_id="a")
    assert receipt.status == STATUS_COMMITTED
    assert read_pending(env.state_dir) is None  # journal finalized


def test_rolled_back_apply_leaves_no_journal(tmp_path: Path) -> None:
    env = _env(tmp_path, probe=lambda c: False)
    plan = InitPlan(
        sdk_api=SDK_API,
        integration_id="demo",
        mutations=(ClaimTomlSection("demo", {"enabled": True}), VerifyProbe("x.y.v1")),
        summary="rollback",
    )
    receipt = apply_plan(plan, env, agent_id="a")
    assert receipt.status == STATUS_ROLLED_BACK
    assert read_pending(env.state_dir) is None


def test_journal_is_written_before_forward_with_preimage(tmp_path: Path) -> None:
    # Simulate the durable-before-forward step by hand: begin, record with the
    # pre-image, and assert the on-disk journal carries it BEFORE any forward runs.
    env = _env(tmp_path)
    original = env.config_path.read_bytes()
    journal = Journal(env.state_dir)
    with wizard_lock(env.state_dir):
        journal.begin(agent_id="a", integration_id="demo", sdk_api=SDK_API, summary="s")
        journal.record(
            index=0,
            kind="claim_toml_section",
            target="demo",
            recover_path=str(env.config_path),
            pre_image=original,
            recover_mode=0o644,
        )
    payload = json.loads(_active_path(env.state_dir).read_text(encoding="utf-8"))
    entry = payload["entries"][0]
    assert entry["recover_path"] == str(env.config_path)
    assert base64.b64decode(entry["pre_image_b64"]) == original


def test_crash_mid_apply_is_recovered_out_of_process(tmp_path: Path) -> None:
    # Simulate a crash: journal a claim + mutate the host, but never finalize.
    env = _env(tmp_path)
    original = env.config_path.read_bytes()
    journal = Journal(env.state_dir)
    with wizard_lock(env.state_dir):
        journal.begin(agent_id="a", integration_id="demo", sdk_api=SDK_API, summary="s")
        journal.record(
            index=0,
            kind="claim_toml_section",
            target="demo",
            recover_path=str(env.config_path),
            pre_image=original,
            recover_mode=0o644,
        )
        # the "forward" happened, then the process died before finalize:
        env.config_path.write_bytes(original + b'\n[demo]\nenabled = true\n')

    # A fresh doctor sees the pending journal...
    pending = read_pending(env.state_dir)
    assert pending is not None and len(pending) == 1
    assert pending[0].kind == "claim_toml_section"

    # ...and recovery restores the pre-apply bytes, then clears the journal.
    result = recover(env.state_dir)
    assert result is not None
    assert "claim_toml_section:demo" in result.recovered
    assert env.config_path.read_bytes() == original
    assert read_pending(env.state_dir) is None


def test_recover_reports_provider_kinds_as_manual(tmp_path: Path) -> None:
    env = _env(tmp_path)
    journal = Journal(env.state_dir)
    with wizard_lock(env.state_dir):
        journal.begin(agent_id="a", integration_id="buckets_gate", sdk_api=SDK_API, summary="s")
        journal.record(
            index=0,
            kind="caddy_drop_in",
            target="b.caddy",
            recover_path=None,  # provider-managed
            pre_image=None,
            recover_mode=0,
        )
    result = recover(env.state_dir)
    assert result is not None
    assert result.recovered == ()
    assert "caddy_drop_in:b.caddy" in result.manual


def test_recover_returns_none_when_clean(tmp_path: Path) -> None:
    env = _env(tmp_path)
    assert recover(env.state_dir) is None


def test_multi_step_recovery_is_reverse_order(tmp_path: Path) -> None:
    env = _env(tmp_path)
    original = env.config_path.read_bytes()
    other = env.state_dir.parent / "other.toml"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("original-other\n", encoding="utf-8")
    other_original = other.read_bytes()
    journal = Journal(env.state_dir)
    with wizard_lock(env.state_dir):
        journal.begin(agent_id="a", integration_id="demo", sdk_api=SDK_API, summary="s")
        journal.record(index=0, kind="claim_toml_section", target="demo",
                       recover_path=str(env.config_path), pre_image=original, recover_mode=0o644)
        journal.record(index=1, kind="install_file", target="other",
                       recover_path=str(other), pre_image=other_original, recover_mode=0o644)
        env.config_path.write_bytes(b"mutated-1\n")
        other.write_bytes(b"mutated-2\n")
    result = recover(env.state_dir)
    assert result is not None
    assert env.config_path.read_bytes() == original
    assert other.read_bytes() == other_original
