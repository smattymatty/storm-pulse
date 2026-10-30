"""``stormpulse`` with no command lists every registered command, read from the parser.

The hand-written list drifted to 13 of 16 commands. Now the text is argparse's
own, so the only way to miss a command is to not register it.
"""

from __future__ import annotations

import argparse
import sys

import pytest

import stormpulse.cli as cli


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    return dict(action.choices)


def _help_on_stderr(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    monkeypatch.setattr(sys, "argv", ["stormpulse", *argv])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 1
    return capsys.readouterr().err


def test_no_command_names_every_registered_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    names = _subcommands(cli.build_parser())
    lost_before = {"logs", "rclone", "integration"}
    assert lost_before <= names.keys()
    err = _help_on_stderr([], monkeypatch, capsys)
    for name in names:
        assert name in err, f"{name} registered but not listed"


def test_a_newly_registered_command_appears_unprompted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def add_extra(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
        subparsers.add_parser("zz-extra", help="registered by the test")

    monkeypatch.setattr(cli, "REGISTRARS", (*cli.REGISTRARS, add_extra))
    assert "zz-extra" in _help_on_stderr([], monkeypatch, capsys)


@pytest.mark.parametrize(
    "group", ["config", "garage", "caddy", "rclone", "logging", "signoff"]
)
def test_a_group_without_a_subcommand_prints_its_own_help(
    group: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    err = _help_on_stderr([group], monkeypatch, capsys)
    assert f"usage: stormpulse {group}" in err
    for leaf in _subcommands(_subcommands(cli.build_parser())[group]):
        assert leaf in err
