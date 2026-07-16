"""CLI: `stormpulse integration doctor` reports the P2 readiness graph alongside P1
package diagnostics, and --recover restores an interrupted wizard apply."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from stormpulse.cli import integration as cli_integ
from stormpulse.wizard.journal import Journal, wizard_lock


def _args(**kw: object) -> argparse.Namespace:
    ns = argparse.Namespace(
        integration_command="doctor", integration_id=None, recover=False, json=True
    )
    for key, value in kw.items():
        setattr(ns, key, value)
    return ns


def test_doctor_reports_readiness(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli_integ.run(_args(), state_dir=tmp_path, agent_id="a", integrations_config={})
    payload = json.loads(capsys.readouterr().out)
    assert payload["operation"] == "doctor"
    # doctor now carries both P1 findings and the P2 readiness graph
    assert "readiness" in payload["result"]
    assert payload["result"]["readiness"]["garage"]["state"] == "available"
    assert payload["result"]["journal_pending"] == 0
    assert code == 0


def test_doctor_recovers_interrupted_apply(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
        _args(recover=True), state_dir=tmp_path, agent_id="a", integrations_config={}
    )
    payload = json.loads(capsys.readouterr().out)
    assert "claim_toml_section:demo" in payload["result"]["recovered"]
    assert cfg.read_bytes() == original  # pre-apply state restored via doctor --recover
    from stormpulse.wizard import read_pending

    assert read_pending(tmp_path) is None
    assert code == 0


def test_readiness_subcommand_is_gone_folded_into_doctor() -> None:
    # The naming deviation was reverted: readiness lives under `doctor`, not its own
    # subcommand. `integration doctor` parses; `integration readiness` is rejected.
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    cli_integ.add_integration_subparser(sub)
    parser.parse_args(["integration", "doctor"])  # ok
    with pytest.raises(SystemExit):
        parser.parse_args(["integration", "readiness"])
