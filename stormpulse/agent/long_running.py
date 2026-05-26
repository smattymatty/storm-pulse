"""Long-running command handler resolution.

Each Feature publishes a name → factory map (see
``stormpulse.garage.long_running_factories`` and
``stormpulse.caddy.long_running_factories``). The agent's
``bootstrap.build_agent_dependencies`` composes them into a single
dict the dispatcher looks commands up in.

This module is a thin adapter: it takes the composed dict and
invokes the right factory with the validated runtime params. Keeping
the resolution behind a function (instead of inlining the dict
lookup at the call site) gives tests a single seam to monkey-patch.
"""

from __future__ import annotations

import logging

from stormpulse.commands.jobs import JobHandler, LongRunningFactory

logger = logging.getLogger(__name__)


def resolve_long_running_handler(
    factories: dict[str, LongRunningFactory],
    command: str,
    params: dict[str, str],
) -> JobHandler | None:
    """Build the handler coroutine for *command*, or return ``None``.

    Returns ``None`` when:

    - The command is not in *factories* — no Feature claims it.
    - The factory itself returns ``None`` — the feature is enabled
      but cannot serve this command on the current install (e.g.
      a required external dep is missing).

    In either case the dispatcher emits a structured no-handler
    failure result so the dashboard sees the rejection.
    """
    factory = factories.get(command)
    if factory is None:
        return None
    return factory(params)
