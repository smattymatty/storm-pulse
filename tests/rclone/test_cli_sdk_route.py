"""CLI: `stormpulse rclone init --sdk` routes through the SDK driver; the default
route stays on the legacy procedural path (the wizard/engine path itself is proven
in test_wizard_port.py)."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from stormpulse.cli import rclone as cli_rclone


def test_sdk_flag_routes_to_sdk_driver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    called: dict[str, object] = {}

    def fake(config_path: Path, *, binary_path_override: str | None, force: bool) -> None:
        called["config"] = config_path
        called["force"] = force

    monkeypatch.setattr(cli_rclone, "_run_rclone_sdk_init", fake)
    ns = argparse.Namespace(
        config=str(tmp_path / "s.toml"), binary_path=None, force=True, sdk=True
    )
    cli_rclone.cmd_rclone_init(ns)
    assert called["config"] == Path(str(tmp_path / "s.toml"))
    assert called["force"] is True


def test_default_route_uses_legacy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    called: dict[str, bool] = {}

    def fake_legacy(config_path: Path, *, binary_path_override: str | None, force: bool) -> None:
        called["legacy"] = True

    monkeypatch.setattr("stormpulse.rclone.init.run_rclone_init", fake_legacy)
    ns = argparse.Namespace(
        config=str(tmp_path / "s.toml"), binary_path=None, force=False, sdk=False
    )
    cli_rclone.cmd_rclone_init(ns)
    assert called.get("legacy") is True
