"""Tests for ``stormpulse logs`` (stormpulse.cli.logs)."""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import pytest

from stormpulse.cli.logs import _journalctl_argv, cmd_logs
from stormpulse.init.mode import InstallMode


class TestJournalctlArgv:
    def test_user_mode_default(self) -> None:
        assert _journalctl_argv(InstallMode.USER, lines=100, follow=True) == [
            "journalctl",
            "--user",
            "-u",
            "stormpulse",
            "-n",
            "100",
            "-f",
        ]

    def test_system_mode_default(self) -> None:
        assert _journalctl_argv(InstallMode.SYSTEM, lines=100, follow=True) == [
            "journalctl",
            "-u",
            "stormpulse",
            "-n",
            "100",
            "-f",
        ]

    def test_no_follow(self) -> None:
        assert _journalctl_argv(InstallMode.USER, lines=50, follow=False) == [
            "journalctl",
            "--user",
            "-u",
            "stormpulse",
            "-n",
            "50",
        ]


class TestCmdLogs:
    def _ns(self, lines: int = 100, follow: bool = True) -> argparse.Namespace:
        return argparse.Namespace(lines=lines, follow=follow)

    def test_clean_exit_returns_normally(self) -> None:
        with patch(
            "stormpulse.cli.logs.subprocess.run",
            return_value=MagicMock(returncode=0),
        ):
            with patch(
                "stormpulse.cli.logs.detect_mode",
                return_value=InstallMode.USER,
            ):
                cmd_logs(self._ns())

    def test_nonzero_exit_propagates(self) -> None:
        with patch(
            "stormpulse.cli.logs.subprocess.run",
            return_value=MagicMock(returncode=130),
        ):
            with patch(
                "stormpulse.cli.logs.detect_mode",
                return_value=InstallMode.USER,
            ):
                with pytest.raises(SystemExit) as exc:
                    cmd_logs(self._ns())
        assert exc.value.code == 130

    def test_journalctl_missing_exits_1(self) -> None:
        with patch(
            "stormpulse.cli.logs.subprocess.run",
            side_effect=FileNotFoundError(),
        ):
            with patch(
                "stormpulse.cli.logs.detect_mode",
                return_value=InstallMode.USER,
            ):
                with pytest.raises(SystemExit) as exc:
                    cmd_logs(self._ns())
        assert exc.value.code == 1

    def test_empty_journal_no_follow_prints_hint(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Regression test for audit finding 1.3: when journalctl returns 0
        with no output (unit not yet started), surface a hint."""
        with patch(
            "stormpulse.cli.logs.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ):
            with patch(
                "stormpulse.cli.logs.detect_mode",
                return_value=InstallMode.USER,
            ):
                cmd_logs(self._ns(follow=False))
        captured = capsys.readouterr()
        assert "No stormpulse logs found" in captured.err
        assert "stormpulse init" in captured.err

    def test_non_empty_journal_no_follow_no_hint(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Hint must NOT fire when the journal does have content."""
        with patch(
            "stormpulse.cli.logs.subprocess.run",
            return_value=MagicMock(
                returncode=0,
                stdout="some log line\n",
                stderr="",
            ),
        ):
            with patch(
                "stormpulse.cli.logs.detect_mode",
                return_value=InstallMode.USER,
            ):
                cmd_logs(self._ns(follow=False))
        captured = capsys.readouterr()
        assert "some log line" in captured.out
        assert "No stormpulse logs found" not in captured.err
