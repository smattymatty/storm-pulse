"""Function 5: the Integration contract (CORE-005 governance): required core
fields, first-party command contribution (decision 8), and disjoint log-enricher
parser keys (decision 13). The runtime sibling of Fn4's static dependency fence.

CORE-007 amends decision 8: a command contributor is first-party OR a sealed
external adapter holding a ``command_contributor`` grant. Only built-ins register
at fitness time (the runtime loader does not run here), so this still asserts
first-party for everything it can see; an external adapter's commands are gated
at load by its grant, not by this static check, and its translated ``specs``
builder lives in the Entry-layer translator (a ``stormpulse`` module), so it
would satisfy the check below anyway."""

from __future__ import annotations

from typing import Any

import stormpulse.agent.integrations_manifest  # noqa: F401  (registers Integrations)
from stormpulse.integrations import registered_integrations


def _about(integ: Any, what: str) -> str:
    """One violation line, always naming the Integration it is about."""
    return f"Integration {integ.id!r}: {what}"


def check_integration_contract() -> list[str]:
    """Return violation strings; empty list means clean."""
    violations: list[str] = []
    enricher_owners: dict[str, str] = {}
    for integ in registered_integrations():
        if not isinstance(integ.id, str) or not integ.id:
            violations.append(
                f"Integration with a non-empty string id required, got {integ.id!r}"
            )
        if not callable(integ.parse_config):
            violations.append(_about(integ, "parse_config must be callable"))
        if not callable(integ.enabled):
            violations.append(_about(integ, "enabled must be callable"))
        if integ.specs is not None:
            module = getattr(integ.specs, "__module__", "")
            if not module.startswith("stormpulse"):
                violations.append(
                    _about(
                        integ,
                        f"contributes commands from non-first-party module {module!r} "
                        "(CORE-005 decision 8 / CORE-007: a built-in contributor is "
                        "first-party; an external one is gated by its "
                        "command_contributor grant at load)",
                    )
                )
        if (integ.specs is not None or integ.collect_state is not None) and (
            integ.declared_config is None
        ):
            violations.append(
                _about(
                    integ,
                    "contributes commands but declares no declared_config, so the "
                    "wire contract cannot list them without a host (CORE-008: every "
                    "accepted command is declared)",
                )
            )
        for parser in integ.log_enrichers or {}:
            owner = enricher_owners.setdefault(parser, integ.id)
            if owner != integ.id:
                violations.append(
                    f"Integrations {owner!r} and {integ.id!r} both declare a "
                    f"log enricher for parser {parser!r} (CORE-005 decision 13: "
                    "parser keys are disjoint)"
                )
    return violations
