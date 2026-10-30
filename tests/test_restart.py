"""Tests for ``stormpulse restart`` (stormpulse.cli.restart)."""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import pytest

from stormpulse.cli.restart import cmd_restart
from stormpulse.init.system import offer_restart, restart_user_unit


class TestRestartUserUnit:
    def test_runs_systemctl_user_restart(self) -> None:
        calls: list[list[str]] = []

        def fake_run(argv: list[str]) -> MagicMock:
            calls.append(list(argv))
            return MagicMock(returncode=0)

        with patch("stormpulse.init.system.subprocess.run", side_effect=fake_run):
            code = restart_user_unit()

        assert code == 0
        assert calls == [["systemctl", "--user", "restart", "stormpulse"]]

    def test_propagates_systemctl_exit_code(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with patch(
            "stormpulse.init.system.subprocess.run",
            return_value=MagicMock(returncode=5),
        ):
            code = restart_user_unit()

        assert code == 5
        assert "exited 5" in caplog.text


class TestOfferRestart:
    """The one restart prompt every feature init ends on (rootless only)."""

    def test_yes_restarts_the_user_unit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("builtins.input", lambda _prompt: "y")
        with patch(
            "stormpulse.init.system.subprocess.run",
            return_value=MagicMock(returncode=0),
        ) as run:
            offer_restart()
        assert [c.args[0] for c in run.call_args_list] == [
            ["systemctl", "--user", "restart", "stormpulse"]
        ]

    def test_no_prints_the_user_command_and_runs_nothing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr("builtins.input", lambda _prompt: "n")
        with patch("stormpulse.init.system.subprocess.run") as run:
            offer_restart()
        run.assert_not_called()
        err = capsys.readouterr().err
        assert "systemctl --user restart stormpulse" in err
        assert "sudo" not in err


class TestCmdRestart:
    def test_success_returns_normally(self) -> None:
        with patch(
            "stormpulse.init.system.subprocess.run",
            return_value=MagicMock(returncode=0),
        ):
            # Should not raise SystemExit on clean restart.
            cmd_restart(argparse.Namespace())

    def test_failure_exits_with_systemctl_code(self) -> None:
        with patch(
            "stormpulse.init.system.subprocess.run",
            return_value=MagicMock(returncode=7),
        ):
            with pytest.raises(SystemExit) as exc:
                cmd_restart(argparse.Namespace())

        assert exc.value.code == 7
