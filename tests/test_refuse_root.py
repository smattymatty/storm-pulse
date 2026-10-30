"""Every stormpulse command refuses UID 0 before it parses an argument (ADR CORE-003).

``refuse_root`` takes the euid as a parameter, so these tests never fake
``os.geteuid`` (Fitness Function 17). The gate in ``main`` is proven with a spy
that refuses, and a parser that fails if it is ever reached.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from stormpulse.cli import main
from stormpulse.init.checks import InitError, refuse_root


def test_euid_zero_is_refused() -> None:
    with pytest.raises(InitError, match="run as the user that owns rootless Docker"):
        refuse_root(0)


def test_non_zero_euid_passes() -> None:
    refuse_root(1000)


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--version"],
        ["init"],
        ["enroll", "https://example.test/api/enroll/", "agent-01", "token"],
        ["run"],
        ["renew"],
        ["update"],
        ["restart"],
        ["signoff", "unseal"],
        ["garage", "init"],
        ["caddy", "init"],
    ],
)
def test_every_command_is_refused_before_parsing(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def refusing(euid: int) -> None:
        calls.append(euid)
        refuse_root(0)

    monkeypatch.setattr("stormpulse.cli.refuse_root", refusing)
    monkeypatch.setattr(sys, "argv", ["stormpulse", *argv])
    with (
        patch(
            "argparse.ArgumentParser.parse_args",
            side_effect=AssertionError("parsed before the root gate"),
        ) as parse,
        pytest.raises(SystemExit) as exited,
    ):
        main()
    assert "will not run as root" in str(exited.value.code)
    assert len(calls) == 1
    parse.assert_not_called()


def test_a_non_root_user_reaches_the_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["stormpulse", "--version"])
    with pytest.raises(SystemExit) as exited:
        main()
    assert exited.value.code == 0
    assert "storm-pulse-agent" in capsys.readouterr().out
