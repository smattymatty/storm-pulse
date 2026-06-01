"""Tests for ``stormpulse restart`` (stormpulse.cli.restart)."""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import pytest

from stormpulse.cli.restart import cmd_restart
from stormpulse.init.mode import InstallMode
from stormpulse.init.system import restart_or_hint


class TestRestartOrHint:
    def test_user_mode_runs_systemctl_user_restart(self) -> None:
        calls: list[list[str]] = []

        def fake_run(argv: list[str]) -> MagicMock:
            calls.append(list(argv))
            return MagicMock(returncode=0)

        with patch("stormpulse.init.system.subprocess.run", side_effect=fake_run):
            code = restart_or_hint(InstallMode.USER)

        assert code == 0
        assert calls == [["systemctl", "--user", "restart", "stormpulse"]]

    def test_user_mode_propagates_systemctl_exit_code(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with patch(
            "stormpulse.init.system.subprocess.run",
            return_value=MagicMock(returncode=5),
        ):
            code = restart_or_hint(InstallMode.USER)

        assert code == 5
        assert "exited 5" in caplog.text

    def test_system_mode_prints_hint_does_not_exec(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with patch("stormpulse.init.system.subprocess.run") as run_mock:
            code = restart_or_hint(InstallMode.SYSTEM)

        # No-escalation posture: never shells out in system mode.
        assert run_mock.call_count == 0
        # Hint is informational, not a failure.
        assert code == 0
        err = capsys.readouterr().err
        assert "System install detected" in err
        assert "systemctl restart stormpulse" in err


class TestCmdRestart:
    def test_user_mode_success_returns_normally(self) -> None:
        with patch(
            "stormpulse.init.system.subprocess.run",
            return_value=MagicMock(returncode=0),
        ):
            with patch(
                "stormpulse.cli.restart.detect_mode",
                return_value=InstallMode.USER,
            ):
                # Should not raise SystemExit on clean restart.
                cmd_restart(argparse.Namespace())

    def test_user_mode_failure_exits_with_systemctl_code(self) -> None:
        with patch(
            "stormpulse.init.system.subprocess.run",
            return_value=MagicMock(returncode=7),
        ):
            with patch(
                "stormpulse.cli.restart.detect_mode",
                return_value=InstallMode.USER,
            ):
                with pytest.raises(SystemExit) as exc:
                    cmd_restart(argparse.Namespace())

        assert exc.value.code == 7

    def test_system_mode_returns_normally_after_hint(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with patch("stormpulse.init.system.subprocess.run") as run_mock:
            with patch(
                "stormpulse.cli.restart.detect_mode",
                return_value=InstallMode.SYSTEM,
            ):
                cmd_restart(argparse.Namespace())

        assert run_mock.call_count == 0
        err = capsys.readouterr().err
        assert "System install detected" in err
