# storm-pulse developer Makefile.
#
# `make check` is the umbrella - run it before pushing. Individual
# targets exist for granular use during work.
#
# Assumes the venv lives at ./.venv. Override PYTHON / LINT_IMPORTS
# for CI or non-venv invocation, e.g. `PYTHON=python3 make check`.

PYTHON ?= .venv/bin/python
LINT_IMPORTS ?= .venv/bin/lint-imports

GARAGE_COMPOSE = docker compose -f docker/garage.test.yml

.PHONY: check test mypy fitness pre-release-check clean \
        garage-up garage-down test-wire test-garage-wire

# Umbrella: every check in one command. No Docker, no network.
check: test mypy fitness

test:
	$(PYTHON) -m pytest -q

# --- wire tier -------------------------------------------------------------
# The real system an Integration drives, not a fake of it. One directory and
# one container per integration under tests/wire/; the `wire` marker keeps the
# whole tier out of `make check`.
#
# Each integration owns a pair of targets: `<name>-up` to boot its container,
# `test-<name>-wire` to run its tests. `test-wire` runs every integration and
# therefore needs every container up.
#
# Version matrix: point one at a candidate build before the fleet takes it.
#   GARAGE_IMAGE=dxflrs/garage:v2.4.0 make garage-up && make test-garage-wire

# Every integration's wire tests. Needs every integration's container up.
test-wire:
	$(PYTHON) -m pytest -m wire -q

# -- garage --
# The harness self-provisions its key and bucket, so there is nothing to set
# up beyond the container. It fails loudly (never skips) if that is missing.

garage-up:
	$(GARAGE_COMPOSE) up -d

garage-down:
	$(GARAGE_COMPOSE) down

test-garage-wire:
	$(PYTHON) -m pytest -m "wire and garage" -q

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
	rm -rf .mypy_cache .pytest_cache .import_linter_cache dist/
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
