"""Tests for stormpulse.init.migrate -- rootful → user mode migration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from stormpulse.init import InitError
from stormpulse.init.migrate import (
    MigrationPlan,
    build_plan,
    check_preconditions,
    translate_toml,
)


def _make_plan(tmp_path: Path) -> MigrationPlan:
    """Build a plan with system + user paths under tmp_path."""
    return MigrationPlan(
        old_config=tmp_path / "etc/stormpulse/stormpulse.toml",
        old_systemd=tmp_path / "etc/systemd/system/stormpulse.service",
        old_creds_dir=tmp_path / "etc/stormpulse",
        new_config=tmp_path / "home/.config/stormpulse/stormpulse.toml",
        new_systemd=tmp_path / "home/.config/systemd/user/stormpulse.service",
        new_creds_dir=tmp_path / "home/.config/stormpulse",
        new_data_dir=tmp_path / "home/.local/share/stormpulse",
    )


class TestBuildPlan:
    def test_returns_plan_with_user_paths(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Pin HOME so user paths are predictable.
        monkeypatch.setenv("HOME", str(tmp_path))
        plan = build_plan()
        assert plan.old_config == Path("/etc/stormpulse/stormpulse.toml")
        assert plan.new_config == tmp_path / ".config/stormpulse/stormpulse.toml"
        assert plan.new_systemd == tmp_path / ".config/systemd/user/stormpulse.service"
        assert plan.new_data_dir == tmp_path / ".local/share/stormpulse"


class TestCheckPreconditions:
    @patch("stormpulse.init.migrate.os.geteuid", return_value=0)
    def test_refuses_when_running_as_root(
        self, _mock: MagicMock, tmp_path: Path
    ) -> None:
        plan = _make_plan(tmp_path)
        with pytest.raises(InitError, match="not via sudo"):
            check_preconditions(plan)

    @patch("stormpulse.init.migrate.os.geteuid", return_value=1000)
    def test_refuses_when_no_rootless_socket(
        self,
        _mock: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        plan = _make_plan(tmp_path)
        with pytest.raises(InitError, match="No rootless docker socket"):
            check_preconditions(plan)

    @patch("stormpulse.init.migrate.os.geteuid", return_value=1000)
    def test_refuses_when_no_old_install(
        self,
        _mock: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        (runtime / "docker.sock").touch()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
        plan = _make_plan(tmp_path)  # old_config does NOT exist
        with pytest.raises(InitError, match="No existing rootful install"):
            check_preconditions(plan)

    @patch("stormpulse.init.migrate.os.geteuid", return_value=1000)
    def test_refuses_when_new_install_already_present(
        self,
        _mock: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        (runtime / "docker.sock").touch()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
        plan = _make_plan(tmp_path)
        # Both old and new configs exist - migration already happened.
        plan.old_config.parent.mkdir(parents=True, exist_ok=True)
        plan.old_config.write_text("[stub]\n")
        plan.new_config.parent.mkdir(parents=True, exist_ok=True)
        plan.new_config.write_text("[stub]\n")
        with pytest.raises(InitError, match="already exists"):
            check_preconditions(plan)

    @patch("stormpulse.init.migrate.os.geteuid", return_value=1000)
    def test_passes_when_environment_ready(
        self,
        _mock: MagicMock,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runtime = tmp_path / "runtime"
        runtime.mkdir()
        (runtime / "docker.sock").touch()
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
        plan = _make_plan(tmp_path)
        plan.old_config.parent.mkdir(parents=True, exist_ok=True)
        plan.old_config.write_text("[stub]\n")
        # new_config does NOT exist
        check_preconditions(plan)  # should not raise


class TestTranslateToml:
    def test_rewrites_cred_paths_to_user_scope(self, tmp_path: Path) -> None:
        plan = _make_plan(tmp_path)
        old = (
            '[tls]\n'
            'ca_cert = "/etc/stormpulse/ca.pem"\n'
            'client_cert = "/etc/stormpulse/agent.pem"\n'
            'client_key = "/etc/stormpulse/agent-key.pem"\n'
            '\n'
            '[auth]\n'
            'hmac_secret = "/etc/stormpulse/hmac.key"\n'
            '\n'
            '[storage]\n'
            'db_path = "/opt/stormpulse/data/stormpulse.db"\n'
        )
        translated = translate_toml(old, plan)
        # No system paths should survive.
        assert "/etc/stormpulse/" not in translated
        assert "/opt/stormpulse/" not in translated
        # All user paths should be present.
        creds = str(plan.new_creds_dir)
        assert f'{creds}/ca.pem' in translated
        assert f'{creds}/agent.pem' in translated
        assert f'{creds}/agent-key.pem' in translated
        assert f'{creds}/hmac.key' in translated
        assert f'{plan.new_data_dir}/stormpulse.db' in translated

    def test_leaves_unrelated_lines_alone(self, tmp_path: Path) -> None:
        plan = _make_plan(tmp_path)
        old = (
            '[agent]\n'
            'id = "stormdevelopments.ca"\n'
            'pulse_token = "uuid-here"\n'
            '\n'
            '[dashboard]\n'
            'url = "wss://stormdevelopments.ca/ws/pulse/"\n'
            'reconnect_max_seconds = 60\n'
        )
        # No paths to translate -> string is unchanged.
        assert translate_toml(old, plan) == old
