# storm-pulse developer Makefile.
#
# `make check` is the umbrella - run it before pushing. Individual
# targets exist for granular use during work.
#
# Assumes the venv lives at ./.venv. Override PYTHON / LINT_IMPORTS
# for CI or non-venv invocation, e.g. `PYTHON=python3 make check`.

PYTHON ?= .venv/bin/python
LINT_IMPORTS ?= .venv/bin/lint-imports

.PHONY: check test mypy fitness pre-release-check clean

# Umbrella: every check in one command.
check: test mypy fitness

test:
	$(PYTHON) -m pytest -q

mypy:
	$(PYTHON) -m mypy .

# CORE-001 fitness suite.
# Function 1 (layer topology) via import-linter; Functions 2-4 via the
# fitness/ runner. See _architecture/adrs/core/001-fitness-functions.md.
fitness:
	$(LINT_IMPORTS)
	$(PYTHON) -m fitness

# CORE-002 release-time check. Asserts pyproject [project].version matches
# the top CHANGELOG.md entry. Run before `uv publish`.
pre-release-check:
	$(PYTHON) scripts/pre_release_check.py

clean:
	rm -rf .mypy_cache .pytest_cache .import_linter_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
