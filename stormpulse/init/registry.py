"""Registry of feature install steps, iterated by the init orchestrator.

Features register here; the orchestrator runs registered steps without
importing any feature. The CORE-000 dependency inversion that keeps the
orchestrator in Framework.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

# An install step receives the path to the freshly-written stormpulse.toml,
# performs its own detection and prompting, and appends its config section.
FeatureInitStep = Callable[[Path], None]

_steps: dict[FeatureInitStep, bool] = {}


def register_init_step(step: FeatureInitStep, *, on_migrate: bool) -> None:
    """Register a feature install step. Called at feature-module import time.

    ``on_migrate`` has no default: every step says whether migrate-to-rootless
    runs it, so a new feature cannot be skipped there by omission.
    """
    _steps.setdefault(step, on_migrate)


def registered_init_steps() -> list[FeatureInitStep]:
    """Return the registered install steps, in registration order."""
    return list(_steps)


def migration_init_steps() -> list[FeatureInitStep]:
    """Return the steps migrate-to-rootless runs against the new config."""
    return [step for step, on_migrate in _steps.items() if on_migrate]
