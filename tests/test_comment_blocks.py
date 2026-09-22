"""Comment gate regressions, including snapshot isolation and real docstring detection."""

import os
import subprocess
from pathlib import Path

import pytest

from scripts.comment_blocks import blocks, changed_blocks, eligible, main


def test_only_actual_docstrings_count():
    source = '\n'.join(
        [
            '"""Module docs."""',
            'DATA = """',
            '# not a comment',
            'fixture',
            '"""',
            'class C:',
            '    """Class docs."""',
            '    async def f(self):',
            '        """Function docs."""',
            '        return """payload"""',
        ]
    )
    assert [(b.kind, b.start, b.length) for b in blocks(source, 'a.py')] == [
        ('docstring', 1, 1),
        ('docstring', 7, 1),
        ('docstring', 9, 1),
    ]


def test_todo_exemptions_and_mentions():
    source = '# TODO(owner): task\n# details\n\n# Explain TODO behavior\n# second\n'
    assert [(b.start, b.length) for b in blocks(source, 'a.py')] == [(4, 2)]
    assert blocks('"""TODO: document later."""', 'a.py') == []
    assert blocks('#!/bin/sh\n# todo: later\n# details', 'a.sh') == []


def test_toml_and_generated_paths_are_exempt():
    assert not eligible('config/example.toml')
    assert not eligible('.venv/lib/example.py')
    assert not eligible('dist/example.py')
    assert eligible('Dockerfile')
    assert eligible('fitness/example.py')


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith('GIT_'):
            monkeypatch.delenv(key)
    run_git(tmp_path, 'init', '-q')
    run_git(tmp_path, 'config', 'user.name', 'Comment test')
    run_git(tmp_path, 'config', 'user.email', 'test@example.invalid')
    return tmp_path


def run_git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, text=True).strip()


def stage(root, path, text):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    run_git(root, 'add', '--', path)


def commit(root):
    run_git(root, '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'fixture')
    return run_git(root, 'rev-parse', 'HEAD')


def test_initial_commit_and_partial_staging(repo):
    stage(repo, 'a space.py', '# a\n# b\n# c\n# d\n')
    (repo / 'a space.py').write_text('# shortened but unstaged\n')
    assert main(['--root', str(repo), '--staged']) == 1
    stage(repo, 'a space.py', '# short\n')
    (repo / 'a space.py').write_text('# a\n# b\n# c\n# d\n')
    assert main(['--root', str(repo), '--staged']) == 0


def test_docstring_continuation_gates_full_block(repo):
    stage(repo, 'a.py', 'def f():\n    """\n' + '    line\n' * 7 + '    """\n')
    commit(repo)
    stage(repo, 'a.py', 'def f():\n    """\n' + '    line\n' * 8 + '    """\n')
    assert main(['--root', str(repo), '--staged']) == 1


def test_fixture_strings_do_not_fail(repo):
    stage(repo, 'fixture.py', 'DATA = """\n' + '# data\n' * 41 + '"""\n')
    assert changed_blocks(repo, staged=True, base=None) == []


def test_todo_context_outside_diff_hunk(repo):
    stage(repo, 'a.py', '# TODO: later\n' + '# details\n' * 20)
    commit(repo)
    stage(repo, 'a.py', '# TODO: later\n' + '# details\n' * 21)
    assert main(['--root', str(repo), '--staged']) == 0


def test_diff_covers_all_commits_and_ignores_worktree(repo):
    stage(repo, 'a.py', '# short\n')
    base = commit(repo)
    stage(repo, 'a.py', '# a\n# b\n# c\n# d\n')
    commit(repo)
    stage(repo, 'b.py', 'x = 1\n')
    commit(repo)
    (repo / 'a.py').write_text('# unstaged fix\n')
    assert main(['--root', str(repo), '--diff', base]) == 1
    assert main(['--root', str(repo), '--all']) == 0


def test_deleted_files_and_invalid_bases(repo):
    stage(repo, 'a.py', '# short\n')
    commit(repo)
    run_git(repo, 'rm', 'a.py')
    assert main(['--root', str(repo), '--staged']) == 0
    assert main(['--root', str(repo), '--diff', 'missing-base']) == 2


def test_invalid_staged_python_fails_closed(repo):
    stage(repo, 'a.py', 'def broken(\n')
    assert main(['--root', str(repo), '--staged']) == 2


def test_real_garage_fixture_is_not_a_docstring():
    path = Path(__file__).parent / 'garage' / 'fixtures.py'
    found = blocks(path.read_text(), str(path))
    assert not any(b.kind == 'docstring' and b.start == 31 for b in found)
