"""Function 16: every emitted event is declared (CORE-011 d6).

Walks the agent for ``emit(`` and ``record_call(`` calls, resolves module
string constants, and checks each (source, kind) and literal field against
the tree's ``declare_event(`` calls. A ``**`` splat or an f-string key is
the runtime's job: ``emit`` drops it and the next drain counts ``undeclared``.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stormpulse.events import ENVELOPE_FIELDS

ROOT = Path(__file__).resolve().parent.parent / "stormpulse"
HOME = "stormpulse/events.py"

Consts = dict[str, ast.AST]
Declared = dict[tuple[str, str], set[str]]


@dataclass(slots=True)
class _Emit:
    at: str
    source: str | None
    kind: str | None
    fields: set[str] = field(default_factory=set)


def check_event_registry(root: Path = ROOT) -> list[str]:
    """Return violation strings; empty list means clean."""
    declared: Declared = {}
    emits: list[_Emit] = []
    for rel, node, consts in _calls(root):
        name = _callee(node.func)
        if name == "declare_event" and len(node.args) == 3:
            _declare(declared, node, consts)
        elif name in ("emit", "record_call") and node.args:
            emit = _emit(rel, node, consts)
            if emit is not None:
                emits.append(emit)
    return [v for emit in emits for v in _violations(emit, declared)]


def _calls(root: Path) -> Iterator[tuple[str, ast.Call, Consts]]:
    """Every call in every module under *root*, with its module's constants."""
    for path in sorted(root.rglob("*.py")):
        rel = str(path.relative_to(root.parent))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        consts = _module_constants(tree)
        calls = (n for n in ast.walk(tree) if isinstance(n, ast.Call))
        yield from ((rel, node, consts) for node in calls)


def _violations(emit: _Emit, declared: Declared) -> list[str]:
    if emit.source is None or emit.kind is None:
        return [f"{emit.at} emits a kind or source that is not a string constant"]
    fields = declared.get((emit.source, emit.kind))
    if fields is None:
        return [f"{emit.at} emits {emit.source}/{emit.kind} nobody declared"]
    return [
        f"{emit.at} emits {emit.source}/{emit.kind} with undeclared field {name}"
        for name in sorted(emit.fields - fields - ENVELOPE_FIELDS)
    ]


def _callee(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return func.attr if func.value.id == "events" else None
    return None


def _declare(declared: Declared, node: ast.Call, consts: Consts) -> None:
    source = _string(node.args[0], consts)
    kind = _string(node.args[1], consts)
    fields = _strings(node.args[2], consts)
    if source is None or kind is None or fields is None:
        return
    declared.setdefault((source, kind), set()).update(fields)


def _emit(rel: str, node: ast.Call, consts: Consts) -> _Emit | None:
    """The call as an emit; None for the home's own forwarding ``emit(kind, source=source)``."""
    keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
    source = keywords.pop("source", None)
    emit = _Emit(
        f"{rel}:{node.lineno}",
        _string(source, consts) if source is not None else None,
        _string(node.args[0], consts),
        set(keywords),
    )
    if rel == HOME and not (emit.source and emit.kind):
        return None
    return emit


def _module_constants(tree: ast.Module) -> Consts:
    """``NAME = <expr>`` at module level, by name; the value is resolved lazily."""
    return {
        node.targets[0].id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    }


def _string(node: ast.AST | None, consts: Consts) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in consts:
        return _string(consts[node.id], consts)
    return None


def _strings(node: ast.AST, consts: Consts) -> set[str] | None:
    """A set of string constants: a set literal, ``frozenset(...)``, a module
    name bound to one, or a ``|`` union of those. Dispatched on node type."""
    resolve = _STRINGS_BY_NODE.get(type(node))
    return resolve(node, consts) if resolve is not None else None


def _set_literal(node: ast.Set, consts: Consts) -> set[str] | None:
    values = [_string(elt, consts) for elt in node.elts]
    return None if None in values else {v for v in values if v is not None}


def _frozenset_call(node: ast.Call, consts: Consts) -> set[str] | None:
    if _callee(node.func) != "frozenset" or len(node.args) != 1:
        return None
    return _strings(node.args[0], consts)


def _bound_name(node: ast.Name, consts: Consts) -> set[str] | None:
    return _strings(consts[node.id], consts) if node.id in consts else None


def _union(node: ast.BinOp, consts: Consts) -> set[str] | None:
    if not isinstance(node.op, ast.BitOr):
        return None
    left, right = _strings(node.left, consts), _strings(node.right, consts)
    return None if left is None or right is None else left | right


_STRINGS_BY_NODE: dict[type, Callable[[Any, Consts], set[str] | None]] = {
    ast.Set: _set_literal,
    ast.Call: _frozenset_call,
    ast.Name: _bound_name,
    ast.BinOp: _union,
}
