"""Install-time hook: ship the agent sealed by default.

Imported for its registration side effect by ``stormpulse.cli.init`` -
following the same dependency-inversion pattern as ``garage.init`` and
``logging.init`` so the install orchestrator (Framework) doesn't have
to import this Feature directly.

See ADR CORE-004 for the rationale: a freshly-enrolled agent must
land in the sealed state so the dashboard's ``run_verify_block``
hatch is closed by default. The operator's first verification action
is an explicit ``stormpulse signoff unseal``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from stormpulse.config import load_config
from stormpulse.init.registry import register_init_step
from stormpulse.signoff import SignoffState, state_dir_from_db_path

logger = logging.getLogger(__name__)


def signoff_init_step(config_path: Path) -> None:
    """Ship the agent sealed: create the seal flag before first run."""
    config = load_config(config_path)
    state_dir = state_dir_from_db_path(config.storage.db_path)
    state_dir.mkdir(parents=True, exist_ok=True)
    state = SignoffState(state_dir)
    if state.seal():
        logger.info("Sealed agent at install time: %s", state.path)


register_init_step(signoff_init_step)
