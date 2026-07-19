"""Investigation case-file types (the ``stormpulse investigate`` contract).

Foundation layer: imports stdlib only. An Integration declares its
investigations against these types; the CLI host owns rendering, exactly
as the init wizard's host owns rendering (CORE-007 I2).

Sealed vocabulary (CONTEXT.md): an Investigation is a one-shot,
non-interactive run producing a Case file - one Verdict per suspect with
its evidence line, then suggested next moves and named open questions.
The guidance lives in the report's prose, never in prompts. A check that
cannot see must say so: INCONCLUSIVE names precisely what is missing and
the command that would supply it (the 2026-07-19 flap hunt's silent
``journalctl -k`` empty-output trap, made impossible by construction).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Verdict(Enum):
    """Per-suspect result. Ruling a suspect out is a first-class finding."""

    CLEARED = "cleared"
    IMPLICATED = "implicated"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class SuspectReport:
    """One suspect's verdict inside a Case file.

    ``evidence`` is the one-line measurement the verdict rests on.
    ``detail`` is optional operator-facing prose explaining what the
    evidence means (the hand-holding). ``remedy`` is required in spirit
    for INCONCLUSIVE: the exact command that would supply the missing
    evidence. Like wizard findings, none of these may carry a secret.
    """

    suspect: str
    verdict: Verdict
    evidence: str
    detail: str = ""
    remedy: str = ""


@dataclass(frozen=True, slots=True)
class Window:
    """The concrete time window an Investigation examines.

    Resolved once by the CLI from the operator's ``--since``/``--until``;
    investigations format it for their own evidence sources (journalctl,
    ``docker logs``, log files) rather than re-parsing operator input.
    """

    since: datetime
    until: datetime | None = None

    @property
    def label(self) -> str:
        fmt = "%Y-%m-%d %H:%M"
        end = self.until.strftime(fmt) if self.until else "now"
        return f"{self.since.strftime(fmt)} → {end}"


@dataclass(frozen=True, slots=True)
class CaseFile:
    """An Investigation's output: verdicts, then guidance.

    ``receipt`` names the incident that earned the investigation - the
    same discipline as the control plane's saved investigations, where
    the receipt is the most valuable line in the file.
    """

    investigation: str
    title: str
    receipt: str
    window: str
    reports: tuple[SuspectReport, ...]
    next_moves: tuple[str, ...] = field(default=())
    open_questions: tuple[str, ...] = field(default=())
