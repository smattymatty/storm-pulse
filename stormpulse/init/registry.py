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

_steps: list[FeatureInitStep] = []


def register_init_step(step: FeatureInitStep) -> None:
    """Register a feature install step. Called at feature-module import time.

    Idempotent: re-registering the same step is a no-op. Python's module
    caching makes double-import rare, but the guard removes a footgun.
    """
    if step not in _steps:
        _steps.append(step)


def registered_init_steps() -> list[FeatureInitStep]:
    """Return the registered install steps, in registration order."""
    return list(_steps)
