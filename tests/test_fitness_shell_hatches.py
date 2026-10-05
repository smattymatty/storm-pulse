"""Function 13 (every shell hatch ships sealed) catches each drift it exists for."""

from __future__ import annotations

from fitness.shell_hatches import check_shell_hatches_sealed
from stormpulse.config import CommandSpec, ParamDef


def _spec(*argv: str) -> CommandSpec:
    text = ParamDef(placeholder="text", default=None, max_bytes=64)
    return CommandSpec(group="g", command=list(argv), timeout=5, params={"text": text})


def test_live_registry_is_clean() -> None:
    assert check_shell_hatches_sealed() == []


def test_unsealed_shell_argv_fails() -> None:
    specs = {"run_anything": _spec("/usr/bin/sh", "-c", "{text}")}
    assert check_shell_hatches_sealed(specs, frozenset()) == [
        "run_anything: argv runs /usr/bin/sh -c but is not in SEALED_COMMANDS"
    ]


def test_shell_without_dash_c_and_sealed_hatch_pass() -> None:
    specs = {
        "run_script": _spec("/bin/bash", "/opt/app/deploy.sh"),
        "run_hatch": _spec("/bin/bash", "-c", "{text}"),
    }
    assert check_shell_hatches_sealed(specs, frozenset({"run_hatch"})) == []


def test_sealed_name_with_no_command_fails() -> None:
    assert check_shell_hatches_sealed({}, frozenset({"run_gone"})) == [
        "run_gone: in SEALED_COMMANDS but not a registered command"
    ]
