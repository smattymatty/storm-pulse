# Comment and docstring gate

From the repository root:

```bash
python3 scripts/comment_blocks.py --all
python3 scripts/comment_blocks.py --staged
python3 scripts/comment_blocks.py --diff BASE
```

Comments allow three lines; actual Python module, class, and function docstrings
allow nine, including their delimiters. Leading `TODO` blocks are exempt.
TOML configuration guides and generated/vendor directories are excluded.
Python tokenization and AST parsing keep fixture strings out of both counts.

`--all` reports the current working tree without failing on legacy length
violations. Parse/read errors still fail. The gates inspect entire blocks touched
by added or replaced lines, so extending an existing oversized block fails too.
Unchanged legacy blocks do not prevent unrelated work.

`--staged` reads the Git index, including on an initial commit; unstaged edits
cannot change its verdict. `--diff BASE` reads committed HEAD and compares it
directly with BASE. For pull requests, CI supplies the merge-base with main;
for pushes, it supplies the previous tip, covering every commit in the push.
An initial push compares against an empty tree. Missing bases fail explicitly.

Install the configured hooks once per clone:

```bash
.venv/bin/pre-commit install
```

The checker needs only Python's standard library. `make comments` reports the
inventory; `make comments-staged` gates the index; `make check` also checks
committed changes against `COMMENT_BASE` (default `origin/main`).
