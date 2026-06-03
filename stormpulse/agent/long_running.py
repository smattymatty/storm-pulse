"""Resolve a long-running command to a JobHandler via its factory; tests monkey-patch this seam."""

from __future__ import annotations

import logging

from stormpulse.commands.jobs import JobHandler, LongRunningFactory

logger = logging.getLogger(__name__)


def resolve_long_running_handler(
    factories: dict[str, LongRunningFactory],
    command: str,
    params: dict[str, str],
) -> JobHandler | None:
    """Build the handler coroutine for *command*, or ``None`` if no Feature claims it or its factory returns ``None``."""
    factory = factories.get(command)
    if factory is None:
        return None
    return factory(params)
