# storm-pulse developer Makefile.
#
# `make check` is the umbrella - run it before pushing. Individual
# targets exist for granular use during work.
#
# Assumes the venv lives at ./.venv. Override PYTHON / LINT_IMPORTS
# for CI or non-venv invocation, e.g. `PYTHON=python3 make check`.

PYTHON ?= .venv/bin/python
LINT_IMPORTS ?= .venv/bin/lint-imports

COMPOSE = docker compose -f docker/docker-compose.test.yml

.PHONY: check test mypy fitness pre-release-check clean \
        garage-up garage-down test-wire

# Umbrella: every check in one command. No Docker, no network.
check: test mypy fitness

test:
	$(PYTHON) -m pytest -q

# --- wire tier -------------------------------------------------------------
# Real Garage, real admin API, real S3. Deselected from `make check` by the
# `garage` marker; this is how you answer "does the agent still work against
# this Garage build" without deploying.
#
# Version matrix: point it at a candidate build before the fleet takes it.
#   GARAGE_IMAGE=dxflrs/garage:v2.4.0 make garage-up && make test-wire

garage-up:
	$(COMPOSE) up -d

garage-down:
	$(COMPOSE) down

# The harness self-provisions its key and bucket, so there is nothing to set
# up beyond the container. It fails loudly (never skips) if that is missing.
test-wire:
	$(PYTHON) -m pytest -m garage -q

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
