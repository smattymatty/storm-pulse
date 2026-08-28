# storm-pulse developer Makefile.
#
# `make check` is the umbrella - run it before pushing. Individual
# targets exist for granular use during work.
#
# Assumes the venv lives at ./.venv. Override PYTHON / LINT_IMPORTS
# for CI or non-venv invocation, e.g. `PYTHON=python3 make check`.

PYTHON ?= .venv/bin/python
LINT_IMPORTS ?= .venv/bin/lint-imports
SKYLOS ?= .venv/bin/skylos

GARAGE_COMPOSE = docker compose -f docker/garage.test.yml

.PHONY: check test mypy fitness deadcode security quality pre-release-check clean \
        wire-contract log-line-contract garage-up garage-down test-wire test-garage-wire

# Umbrella: every check in one command. No Docker, no network (except
# `security`, whose AI-defect checks may consult the PyPI registry).
check: test mypy fitness deadcode security quality

# Dead-code gate (Skylos), scoped to unused functions / imports / variables /
# classes / files (SKY-U001..U005). SKY-U006 (unused parameters) stays out:
# callback signatures (ws, args, context) are interface conformance, not dead
# code. Deliberate keepers carry inline `# skylos: ignore[...]` with a reason.
deadcode:
	$(SKYLOS) . --select SKY-U001,SKY-U002,SKY-U003,SKY-U004,SKY-U005 --format concise

# Security + secrets + AI-defect gate (Skylos). Exit code comes from the
# zero-tolerance [tool.skylos.gate] policy in pyproject.toml, which counts
# only these categories, so dead-code output cannot redden this gate.
# Globally excluded families and their reasons live in [tool.skylos];
# deliberate keepers carry inline `# skylos: ignore[...]` with a reason.
security:
	$(SKYLOS) . --danger --secrets --ai-defects --sca --gate --format concise

# Quality gate (Skylos), scoped to COMMITS ahead of origin/main so new code
# meets the bar while the legacy findings stay a known baseline. Skylos
# resolves --diff via `git diff origin/main...HEAD`, so uncommitted edits are
# invisible to it, and an empty diff falls back to a FULL scan (red on the
# baseline); the guard below skips the scan in that case instead. SKY-L009
# (print) is globally ignored in [tool.skylos]: Pulse's Case files and
# wizards print by design.
quality:
	@if [ -z "$$(git diff --name-only origin/main...HEAD)" ]; then \
		echo "quality: no commits ahead of origin/main, skipping"; \
	else \
		$(SKYLOS) . --quality --diff origin/main --gate --format concise; \
	fi

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
# Function 1 (layer topology) via import-linter; Functions 2-9 via the
# fitness/ runner. See _architecture/adrs/core/001-fitness-functions.md.
fitness:
	$(LINT_IMPORTS)
	$(PYTHON) -m fitness

# CORE-008: regenerate the declared wire shape from the live dataclasses.
# Run after changing anything an Integration emits, and commit the result:
# Function 9 fails the suite while the artifact and the classes disagree.
# Exits 1 when the file changed, so CI can use it as a forgot-to-regenerate
# check. The diff is a change to a published contract; review it as one.
wire-contract:
	$(PYTHON) -m scripts.generate_wire_contract

# The log-line contract, published SEPARATELY from wire-contract.json on
# purpose: that artifact's digest gates a destructive-sweep refusal on the
# consumer side, and a logging-field change must never be able to pause a key
# reconcile. Same review discipline, no runtime blast radius.
log-line-contract:
	$(PYTHON) -m scripts.generate_log_line_contract

# CORE-002 release-time check. Asserts pyproject [project].version matches
# the top CHANGELOG.md entry. Run before `uv publish`.
pre-release-check:
	$(PYTHON) scripts/pre_release_check.py

clean:
	rm -rf .mypy_cache .pytest_cache .import_linter_cache dist/
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
