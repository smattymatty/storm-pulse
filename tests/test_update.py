"""Tests for ``stormpulse update`` (stormpulse.cli.update)."""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import pytest

from stormpulse.cli.update import GIT_URL, PACKAGE_NAME, _build_pipx_argv, cmd_update


def _ns(**kwargs: object) -> argparse.Namespace:
    """Build an argparse.Namespace for cmd_update with sane defaults.

    Mirrors the argparser's defaults so each test only overrides what
    it cares about.
    """
    defaults: dict[str, object] = {
        "source": "git",
        "branch": None,
        "version": None,
        "restart": True,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


class TestBuildPipxArgv:
    def test_git_default_branch_is_main(self) -> None:
        argv = _build_pipx_argv("git", None, None)
        assert argv == ["pipx", "install", "--force", f"{GIT_URL}@main"]

    def test_git_with_explicit_branch(self) -> None:
        argv = _build_pipx_argv("git", "develop", None)
        assert argv == ["pipx", "install", "--force", f"{GIT_URL}@develop"]

    def test_pip_latest(self) -> None:
        argv = _build_pipx_argv("pip", None, None)
        assert argv == ["pipx", "install", "--force", PACKAGE_NAME]

    def test_pip_pinned_version(self) -> None:
        argv = _build_pipx_argv("pip", None, "0.1.9")
        assert argv == ["pipx", "install", "--force", f"{PACKAGE_NAME}==0.1.9"]

    def test_unknown_source_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown source"):
            _build_pipx_argv("github", None, None)


class TestCmdUpdateFlagRejection:
    def test_branch_with_pip_source_rejects(self, caplog: pytest.LogCaptureFixture) -> None:
        with pytest.raises(SystemExit) as exc:
            cmd_update(_ns(source="pip", branch="main"))
        assert exc.value.code == 2
        assert "--branch is only valid with --source git" in caplog.text

    def test_version_with_git_source_rejects(self, caplog: pytest.LogCaptureFixture) -> None:
        with pytest.raises(SystemExit) as exc:
            cmd_update(_ns(source="git", version="0.1.9"))
        assert exc.value.code == 2
        assert "--version is only valid with --source pip" in caplog.text


class TestCmdUpdatePipxMissing:
    def test_errors_when_pipx_not_on_path(self, caplog: pytest.LogCaptureFixture) -> None:
        with patch("stormpulse.cli.update.shutil.which", return_value=None):
            with pytest.raises(SystemExit) as exc:
                cmd_update(_ns())
        assert exc.value.code == 1
        assert "pipx not found on PATH" in caplog.text
        assert "sudo apt install pipx" in caplog.text


class TestCmdUpdateRestartFlow:
    def test_user_mode_runs_pipx_then_systemctl_user_restart(self) -> None:
        from stormpulse.init.mode import InstallMode
        calls: list[list[str]] = []

        def fake_run(argv: list[str]) -> MagicMock:
            calls.append(list(argv))
            return MagicMock(returncode=0)

        with patch("stormpulse.cli.update.shutil.which", return_value="/usr/bin/pipx"):
            with patch("stormpulse.cli.update.subprocess.run", side_effect=fake_run):
                with patch(
                    "stormpulse.cli.update.detect_mode",
                    return_value=InstallMode.USER,
                ):
                    cmd_update(_ns())

        # Two subprocess calls: pipx install, then systemctl --user restart.
        assert len(calls) == 2
        assert calls[0][:3] == ["pipx", "install", "--force"]
        assert calls[0][3] == f"{GIT_URL}@main"
        assert calls[1] == ["systemctl", "--user", "restart", "stormpulse"]

    def test_system_mode_skips_auto_restart(self, capsys: pytest.CaptureFixture[str]) -> None:
        from stormpulse.init.mode import InstallMode
        calls: list[list[str]] = []

        def fake_run(argv: list[str]) -> MagicMock:
            calls.append(list(argv))
            return MagicMock(returncode=0)

        with patch("stormpulse.cli.update.shutil.which", return_value="/usr/bin/pipx"):
            with patch("stormpulse.cli.update.subprocess.run", side_effect=fake_run):
                with patch(
                    "stormpulse.cli.update.detect_mode",
                    return_value=InstallMode.SYSTEM,
                ):
                    cmd_update(_ns())

        # Only pipx ran; systemctl is the operator's job in system mode.
        assert len(calls) == 1
        assert calls[0][:3] == ["pipx", "install", "--force"]
        err = capsys.readouterr().err
        assert "System install detected" in err
        assert "systemctl restart stormpulse" in err

    def test_no_restart_skips_systemctl(self, capsys: pytest.CaptureFixture[str]) -> None:
        calls: list[list[str]] = []

        def fake_run(argv: list[str]) -> MagicMock:
            calls.append(list(argv))
            return MagicMock(returncode=0)

        with patch("stormpulse.cli.update.shutil.which", return_value="/usr/bin/pipx"):
            with patch("stormpulse.cli.update.subprocess.run", side_effect=fake_run):
                cmd_update(_ns(restart=False))

        # Only pipx ran; restart was opted out.
        assert len(calls) == 1
        err = capsys.readouterr().err
        assert "Skipping restart" in err
        assert "systemctl --user restart stormpulse" in err

    def test_pipx_failure_propagates_exit_code(self) -> None:
        with patch("stormpulse.cli.update.shutil.which", return_value="/usr/bin/pipx"):
            with patch(
                "stormpulse.cli.update.subprocess.run",
                return_value=MagicMock(returncode=42),
            ):
                with pytest.raises(SystemExit) as exc:
                    cmd_update(_ns())
        assert exc.value.code == 42

    def test_systemctl_failure_propagates_exit_code(self) -> None:
        from stormpulse.init.mode import InstallMode
        run_results = iter([
            MagicMock(returncode=0),   # pipx succeeded
            MagicMock(returncode=5),   # systemctl --user failed
        ])
        with patch("stormpulse.cli.update.shutil.which", return_value="/usr/bin/pipx"):
            with patch(
                "stormpulse.cli.update.subprocess.run",
                side_effect=lambda *_a, **_k: next(run_results),
            ):
                with patch(
                    "stormpulse.cli.update.detect_mode",
                    return_value=InstallMode.USER,
                ):
                    with pytest.raises(SystemExit) as exc:
                        cmd_update(_ns())
        assert exc.value.code == 5
