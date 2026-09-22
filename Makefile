# Run make check before pushing; individual targets support focused checks.
# Defaults to .venv; override tools as needed, e.g. PYTHON=python3 make check.

PYTHON ?= .venv/bin/python
LINT_IMPORTS ?= .venv/bin/lint-imports
SKYLOS ?= .venv/bin/skylos

GARAGE_COMPOSE = docker compose -f docker/garage.test.yml

.PHONY: check test mypy fitness deadcode security quality pre-release-check clean \
        wire-contract log-line-contract garage-up garage-down test-wire test-garage-wire

# Umbrella: every check in one command. No Docker, no network (except
# `security`, whose AI-defect checks may consult the PyPI registry).
check: test mypy fitness deadcode security quality comments-diff

# Check unused functions, imports, variables, classes, and files (SKY-U001..U005).
# Exclude parameters: callbacks must preserve interface signatures.
# Explain deliberate exceptions with inline skylos: ignore[...] comments.
deadcode:
	$(SKYLOS) . --select SKY-U001,SKY-U002,SKY-U003,SKY-U004,SKY-U005 --format concise

# Security, secrets, and AI-defect gate; pyproject.toml defines zero tolerance.
# See [tool.skylos] for exclusions; explain inline ignores with a reason.
security:
	$(SKYLOS) . --danger --secrets --ai-defects --sca --gate --format concise

# Check committed changes ahead of origin/main; uncommitted edits are excluded.
# Skip empty diffs because Skylos would scan the entire legacy baseline.
# SKY-L009 (print) is ignored globally for CLI output and wizards.
quality:
	@if [ -z "$$(git diff --name-only origin/main...HEAD)" ]; then \
		echo "quality: no commits ahead of origin/main, skipping"; \
	else \
		$(SKYLOS) . --quality --diff origin/main --gate --format concise; \
	fi

test:
	$(PYTHON) -m pytest -q

# Wire tests use real containers under tests/wire/ and are excluded from make check.
# Each integration supplies <name>-up and test-<name>-wire targets.

# Every integration's wire tests. Needs every integration's container up.
test-wire:
	$(PYTHON) -m pytest -m wire -q

# Garage tests provision their own key and bucket; a missing container fails.
# Test candidate builds with:
# GARAGE_IMAGE=dxflrs/garage:v2.4.0 make garage-up && make test-garage-wire

garage-up:
	$(GARAGE_COMPOSE) up -d

garage-down:
	$(GARAGE_COMPOSE) down

test-garage-wire:
	$(PYTHON) -m pytest -m "wire and garage" -q

mypy:
	$(PYTHON) -m mypy .

# CORE-001: import-linter checks topology; fitness/ runs the remaining checks.
# See _architecture/adrs/core/001-fitness-functions.md.
fitness:
	$(LINT_IMPORTS)
	$(PYTHON) -m fitness

# CORE-008: regenerate after changing Integration dataclasses; review and commit.
# Exits 1 when the artifact changes; fitness rejects stale contracts.
wire-contract:
	$(PYTHON) -m scripts.generate_wire_contract

# Publish log fields separately from the runtime wire-contract.json digest,
# so logging changes cannot block destructive reconciliation. Review the diff.
log-line-contract:
	$(PYTHON) -m scripts.generate_log_line_contract

# CORE-002 release-time check. Asserts pyproject [project].version matches
# the top CHANGELOG.md entry. Run before `uv publish`.
pre-release-check:
	$(PYTHON) scripts/pre_release_check.py

clean:
	rm -rf .mypy_cache .pytest_cache .import_linter_cache dist/
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

# Comment inventory is advisory; staged/committed gates reject changed oversized blocks.
COMMENT_BASE ?= origin/main
.PHONY: comments comments-staged comments-diff
comments:
	$(PYTHON) scripts/comment_blocks.py --all

comments-staged:
	$(PYTHON) scripts/comment_blocks.py --staged

comments-diff:
	$(PYTHON) scripts/comment_blocks.py --diff $(COMMENT_BASE)
