"""Every job's declared result shape (``CommandSpec.result``, CORE-011 d9) names
exactly the keys its module puts in ``JobOutcome.extras``: the declaration lives
beside the producer, and this reads both so neither drifts from the other."""

from __future__ import annotations

import ast
import inspect
from dataclasses import fields
from pathlib import Path

import pytest

from stormpulse.agent.wire_contract import all_command_specs
from stormpulse.config import CommandSpec

_JOBS = {name: spec for name, spec in all_command_specs().items() if spec.mode == "job"}


def _is_extras(node: ast.expr) -> bool:
    """``extras`` or ``<anything>.extras``: the dict a job returns its figures in."""
    return (isinstance(node, ast.Name) and node.id == "extras") or (
        isinstance(node, ast.Attribute) and node.attr == "extras"
    )


def _key(node: ast.expr | None) -> set[str]:
    """A string literal as a one-key set, anything else as none."""
    return (
        {node.value}
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        else set()
    )


def _dict_keys(node: ast.expr | None) -> set[str]:
    return (
        set().union(*(_key(k) for k in node.keys))
        if isinstance(node, ast.Dict)
        else set()
    )


def extras_keys(module: Path) -> set[str]:
    """The top-level keys a module writes into an extras dict, by every form the
    tree uses: ``extras={...}`` and ``extras_extra={...}`` keywords, an ``extras =
    {...}`` assignment, ``extras["k"] = ...`` and ``extras.setdefault("k", ...)``."""
    keys: set[str] = set()
    for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
        if isinstance(node, ast.keyword) and node.arg in ("extras", "extras_extra"):
            keys |= _dict_keys(node.value)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == "extras":
                    keys |= _dict_keys(node.value)
                elif isinstance(target, ast.Subscript) and _is_extras(target.value):
                    keys |= _key(target.slice)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setdefault"
            and _is_extras(node.func.value)
        ):
            keys |= _key(node.args[0])
    return keys


def test_every_job_declares_a_result() -> None:
    """A job returns figures; a job with none to return declares an empty class."""
    assert [name for name, spec in _JOBS.items() if spec.result is None] == []


@pytest.mark.parametrize("name", sorted(_JOBS), ids=sorted(_JOBS))
def test_declared_result_names_exactly_the_keys_its_module_emits(name: str) -> None:
    spec: CommandSpec = _JOBS[name]
    assert spec.result is not None
    module = Path(inspect.getsourcefile(spec.result) or "")
    declared = {f.name for f in fields(spec.result)}
    assert declared == extras_keys(module), (
        f"{name}: {spec.result.__name__} vs {module.name}"
    )
