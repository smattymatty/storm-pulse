"""Report comment/docstring lengths, or gate changed blocks in commits/the index.
Comments allow three lines, docstrings nine; leading TODO blocks and TOML are exempt.
"""

from __future__ import annotations

import argparse
import ast
import io
import os
import re
import subprocess
import sys
import tokenize
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

MAX_LINES = 3
DOC_MAX_LINES = 9
EXTENSIONS = {'.py', '.yml', '.yaml', '.sh', '.cfg', '.ini'}
SKIP = {
    '.git',
    '.venv',
    'venv',
    '__pycache__',
    '.pytest_cache',
    '.mypy_cache',
    '.ruff_cache',
    'dist',
    'build',
    'node_modules',
    '.claude',
    'storm_pulse_agent.egg-info',
}
TODO = re.compile(r'^TODO\b', re.IGNORECASE)
HUNK = re.compile(r'^@@ .* \+(\d+)(?:,(\d+))? @@')


@dataclass(frozen=True)
class Block:
    kind: str
    start: int
    end: int

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    @property
    def limit(self) -> int:
        return DOC_MAX_LINES if self.kind == 'docstring' else MAX_LINES


def eligible(path: str) -> bool:
    p = Path(path)
    return not any(part in SKIP for part in p.parts) and (
        p.suffix in EXTENSIONS
        or p.name == 'Makefile'
        or p.name.startswith('Dockerfile')
    )


def blocks(source: str, path: str) -> list[Block]:
    """Tokenize Python comments and inspect actual AST docstrings, never string fixtures."""
    lines = source.splitlines()
    comments: dict[int, str] = {}
    found: list[Block] = []
    if path.endswith('.py'):
        tree = ast.parse(source, filename=path)
        for node in ast.walk(tree):
            if not isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue
            if not node.body or ast.get_docstring(node, clean=False) is None:
                continue
            doc = node.body[0]
            assert isinstance(doc, ast.Expr) and isinstance(doc.value, ast.Constant)
            value = str(doc.value.value).lstrip()
            if not TODO.match(value):
                found.append(
                    Block('docstring', doc.lineno, doc.end_lineno or doc.lineno)
                )
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                row, col = token.start
                if not lines[row - 1][:col].strip() and not token.string.startswith(
                    '#!'
                ):
                    comments[row] = token.string[1:].strip()
    else:
        for row, line in enumerate(lines, 1):
            text = line.lstrip()
            if text.startswith('#') and not text.startswith('#!'):
                comments[row] = text[1:].strip()
    start = end = 0
    todo = False
    for row in [*sorted(comments), len(lines) + 2]:
        if start and row != end + 1:
            if not todo:
                found.append(Block('comment', start, end))
            start = 0
        if row in comments:
            if not start:
                start = row
                todo = bool(TODO.match(comments[row]))
            end = row
    return sorted(found, key=lambda b: b.start)


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(
        ['git', *args], cwd=root, text=True, encoding='utf-8'
    )


def added_lines(diff: str) -> set[int]:
    """Read new-side hunk ranges from a zero-context diff."""
    result: set[int] = set()
    for line in diff.splitlines():
        match = HUNK.match(line)
        if match:
            start = int(match[1])
            count = int(match[2]) if match[2] is not None else 1
            result.update(range(start, start + count))
    return result


def changed_blocks(
    root: Path, staged: bool, base: str | None
) -> list[tuple[str, Block]]:
    """Gate whole blocks touched by additions, reading exactly the index or HEAD snapshot."""
    if staged:
        target = ['--cached']
        snapshot = ''
    else:
        if base is None:
            raise ValueError('--diff requires a base revision')
        resolved = git(root, 'rev-parse', '--verify', base).strip()
        target = [resolved, 'HEAD']
        snapshot = 'HEAD'
    args = ['diff', '--no-ext-diff', '--no-textconv', '--no-renames', *target]
    paths = git(root, *args, '--name-only', '--diff-filter=ACM', '-z').split('\0')
    found: list[tuple[str, Block]] = []
    for path in paths:
        if not path or not eligible(path):
            continue
        source = git(root, 'show', f'{snapshot}:{path}')
        changed = added_lines(git(root, *args, '--no-color', '--unified=0', '--', path))
        for block in blocks(source, path):
            if any(line in changed for line in range(block.start, block.end + 1)):
                found.append((path, block))
    return found


def inventory(root: Path) -> list[tuple[str, Block]]:
    found: list[tuple[str, Block]] = []
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP)
        for name in sorted(files):
            path = Path(directory) / name
            rel = path.relative_to(root).as_posix()
            if eligible(rel) and not path.is_symlink():
                source = path.read_text(encoding='utf-8')
                found.extend((rel, block) for block in blocks(source, rel))
    return found


def report(found: list[tuple[str, Block]], label: str) -> None:
    for kind in ['comment', 'docstring']:
        selected = [(p, b) for p, b in found if b.kind == kind]
        counts = Counter(b.length for _, b in selected)
        over = sum(b.length > b.limit for _, b in selected)
        print(f'{kind}-blocks ({label}): {len(selected)} blocks, {over} over the limit')
        print('  histogram: ' + ', '.join(f'{n}:{counts[n]}' for n in sorted(counts)))
        if selected:
            path, block = max(selected, key=lambda item: item[1].length)
            print(f'  Worst offender: {path}:{block.start} ({block.length} lines)')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--staged', action='store_true')
    scope.add_argument(
        '--diff', metavar='BASE', help='gate BASE to HEAD; use a merge-base for PRs'
    )
    parser.add_argument(
        '--all', action='store_true', help='inventory only; legacy findings do not fail'
    )
    parser.add_argument('--root', type=Path, default=Path('.'))
    args = parser.parse_args(argv)
    if not (args.all or args.staged or args.diff):
        parser.error('give --all, --staged, or --diff BASE')
    try:
        if args.all:
            report(inventory(args.root), 'whole tree')
        if args.staged or args.diff:
            found = changed_blocks(args.root, args.staged, args.diff)
            report(found, 'staged' if args.staged else f'changed since {args.diff}')
            bad = [(p, b) for p, b in found if b.length > b.limit]
            for path, block in bad:
                print(
                    f'  {path}:{block.start}: {block.length}-line {block.kind} (max {block.limit})'
                )
            return int(bool(bad))
    except (
        OSError,
        UnicodeError,
        SyntaxError,
        tokenize.TokenError,
        subprocess.CalledProcessError,
        ValueError,
    ) as exc:
        print(f'comment-blocks: unable to complete check: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
