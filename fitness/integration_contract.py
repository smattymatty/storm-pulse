"""Function 5: the Integration contract (CORE-005 governance): required core
fields, first-party command contribution (decision 8), and disjoint log-enricher
parser keys (decision 13). The runtime sibling of Fn4's static dependency fence."""

from __future__ import annotations

import stormpulse.agent.integrations_manifest  # noqa: F401  (registers Integrations)
from stormpulse.integrations import registered_integrations


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
            violations.append(f"Integration {integ.id!r}: parse_config must be callable")
        if not callable(integ.enabled):
            violations.append(f"Integration {integ.id!r}: enabled must be callable")
        if integ.specs is not None:
            module = getattr(integ.specs, "__module__", "")
            if not module.startswith("stormpulse"):
                violations.append(
                    f"Integration {integ.id!r}: contributes commands from "
                    f"non-first-party module {module!r} (CORE-005 decision 8: "
                    "command contribution is first-party-only)"
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
