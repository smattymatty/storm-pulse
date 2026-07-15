"""SDK wizard findings (CORE-007 decision 5).

Foundation layer: imports nothing intra-package. These are the ``ok`` /
``warning`` / ``refusal`` findings a wizard's ``inspect`` returns, distinct from
the external-loader diagnostic ``Finding`` in ``integrations/external/model.py``
(a different type in a different layer; the two MUST NOT be merged).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Severity(Enum):
    """SDK finding severity. ``refusal`` blocks the plan; ``ok``/``warning`` inform."""

    OK = "ok"
    WARNING = "warning"
    REFUSAL = "refusal"


# Output ordering: refusal first, then warning, then ok (most-severe-first).
_SEVERITY_RANK = {Severity.REFUSAL: 0, Severity.WARNING: 1, Severity.OK: 2}


def severity_rank(severity: Severity) -> int:
    """Sort key for a finding's severity (lower sorts first: refusal < warning < ok)."""
    return _SEVERITY_RANK[severity]


@dataclass(frozen=True, slots=True)
class Finding:
    """One wizard finding: a severity, a message, and an optional repair step.

    ``repair`` is an operator-facing command or instruction and MUST NOT carry a
    secret. A ``refusal`` finding blocks plan application.
    """

    severity: Severity
    message: str
    repair: str | None = None
