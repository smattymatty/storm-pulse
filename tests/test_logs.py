"""Tests for ``stormpulse logs`` (stormpulse.cli.logs)."""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import pytest

from stormpulse.cli.logs import _journalctl_argv, _resolve_view, cmd_logs
from stormpulse.init.mode import InstallMode


def _ns(
    lines: int | None = None,
    follow: bool | None = None,
    since: str | None = None,
    until: str | None = None,
    grep: str | None = None,
) -> argparse.Namespace:
    """Namespace as the argparse wiring produces it (all-None defaults)."""
    return argparse.Namespace(
        lines=lines, follow=follow, since=since, until=until, grep=grep
    )


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

    def test_window_and_grep_passthrough(self) -> None:
        assert _journalctl_argv(
            InstallMode.SYSTEM,
            lines=None,
            follow=False,
            since="06:00",
            until="07:10",
            grep="1011|Reconnecting",
        ) == [
            "journalctl",
            "-u",
            "stormpulse",
            "--since",
            "06:00",
            "--until",
            "07:10",
            "-g",
            "1011|Reconnecting",
        ]

    def test_no_lines_cap_when_none(self) -> None:
        argv = _journalctl_argv(InstallMode.USER, lines=None, follow=False)
        assert "-n" not in argv


class TestResolveView:
    def test_bare_invocation_keeps_historical_default(self) -> None:
        assert _resolve_view(_ns()) == (100, True)

    def test_since_switches_to_full_window_dump(self) -> None:
        assert _resolve_view(_ns(since="1 hour ago")) == (None, False)

    def test_until_alone_also_bounds(self) -> None:
        assert _resolve_view(_ns(until="07:10")) == (None, False)

    def test_grep_alone_stays_live_tail(self) -> None:
        """A pattern without a window is a filtered live tail."""
        assert _resolve_view(_ns(grep="keepalive")) == (100, True)

    def test_explicit_follow_wins_over_window(self) -> None:
        assert _resolve_view(_ns(since="06:00", follow=True)) == (None, True)

    def test_explicit_lines_wins_over_window(self) -> None:
        assert _resolve_view(_ns(since="06:00", lines=20)) == (20, False)

    def test_explicit_no_follow_wins_over_bare_default(self) -> None:
        assert _resolve_view(_ns(follow=False)) == (100, False)


class TestCmdLogs:
    def test_clean_exit_returns_normally(self) -> None:
        with patch(
            "stormpulse.cli.logs.subprocess.run",
            return_value=MagicMock(returncode=0),
        ):
            with patch(
                "stormpulse.cli.logs.detect_mode",
                return_value=InstallMode.USER,
            ):
                cmd_logs(_ns())

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
                    cmd_logs(_ns())
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
                    cmd_logs(_ns())
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
                cmd_logs(_ns(follow=False))
        captured = capsys.readouterr()
        assert "No stormpulse logs found" in captured.err
        assert "stormpulse init" in captured.err

    def test_empty_filtered_dump_hints_at_filter_not_init(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An empty --since/--grep dump means nothing matched, not a dead
        unit - the init hint would send the operator the wrong way."""
        with patch(
            "stormpulse.cli.logs.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="", stderr=""),
        ):
            with patch(
                "stormpulse.cli.logs.detect_mode",
                return_value=InstallMode.USER,
            ):
                cmd_logs(_ns(since="06:00", grep="zzz"))
        captured = capsys.readouterr()
        assert "matched" in captured.err
        assert "stormpulse init" not in captured.err

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
                cmd_logs(_ns(follow=False))
        captured = capsys.readouterr()
        assert "some log line" in captured.out
        assert "No stormpulse logs found" not in captured.err

    def test_window_flags_reach_journalctl(self) -> None:
        run_mock = MagicMock(return_value=MagicMock(returncode=0, stdout="x\n", stderr=""))
        with patch("stormpulse.cli.logs.subprocess.run", run_mock):
            with patch(
                "stormpulse.cli.logs.detect_mode",
                return_value=InstallMode.SYSTEM,
            ):
                cmd_logs(_ns(since="06:00", until="07:10", grep="1011"))
        argv = run_mock.call_args[0][0]
        assert ["--since", "06:00", "--until", "07:10", "-g", "1011"] == argv[-6:]
        assert "-f" not in argv
        assert "-n" not in argv
