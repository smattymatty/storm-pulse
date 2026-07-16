"""CLI: `stormpulse integration readiness` reports the readiness graph and, with
--recover, recovers an interrupted wizard apply from its journal."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from stormpulse.cli import integration as cli_integ
from stormpulse.wizard.journal import Journal, wizard_lock


def _args(**kw: object) -> argparse.Namespace:
    ns = argparse.Namespace(
        integration_command="readiness", integration_id=None, recover=False, json=False
    )
    for key, value in kw.items():
        setattr(ns, key, value)
    return ns


def test_readiness_json_lists_registered_integrations(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli_integ.run(_args(json=True), state_dir=tmp_path, agent_id="a", integrations_config={})
    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "readiness"
    # the built-in manifest registers garage/caddy/rclone; none configured -> available
    assert "garage" in payload["result"]["readiness"]
    assert payload["result"]["readiness"]["garage"]["state"] == "available"
    assert payload["result"]["journal_pending"] == 0
    assert code == 0  # nothing enabled-but-not-ready


def test_readiness_recover_restores_and_clears(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cfg = tmp_path / "f.toml"
    cfg.write_text("orig\n", encoding="utf-8")
    original = cfg.read_bytes()
    journal = Journal(tmp_path)
    with wizard_lock(tmp_path):
        journal.begin(agent_id="a", integration_id="demo", sdk_api=1, summary="s")
        journal.record(
            index=0,
            kind="claim_toml_section",
            target="demo",
            recover_path=str(cfg),
            pre_image=original,
            recover_mode=0o644,
        )
        cfg.write_bytes(b"mutated\n")

    code = cli_integ.run(
        _args(recover=True, json=True), state_dir=tmp_path, agent_id="a", integrations_config={}
    )
    payload = json.loads(capsys.readouterr().out)
    assert "claim_toml_section:demo" in payload["result"]["recovered"]
    assert cfg.read_bytes() == original  # pre-apply state restored via the CLI
    # journal_pending reports the interrupted apply that WAS found (1), then --recover
    # cleared it, so a fresh read now sees nothing.
    assert payload["result"]["journal_pending"] == 1
    from stormpulse.wizard import read_pending

    assert read_pending(tmp_path) is None
    assert code == 0
