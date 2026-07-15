"""SDK wizard questions and answers (CORE-007 decision 5).

Foundation layer: imports nothing intra-package. Typed question data a wizard
declares; the host renders, validates, and collects answers. A ``secret_ref``
question and its answer carry a reference the host resolves, never secret bytes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class QuestionKind(Enum):
    """The v1 question kinds (CORE-007 D5). ``discovered`` is read-only: the host
    renders a value it found, the operator does not answer it."""

    TEXT = "text"
    CONFIRM = "confirm"
    CHOICE = "choice"
    SECRET_REF = "secret_ref"
    INTEGER = "integer"
    PATH = "path"
    PORT = "port"
    DISCOVERED = "discovered"


@dataclass(frozen=True, slots=True)
class Question:
    """One typed question. ``key`` identifies the answer; ``choices`` is required
    iff ``kind`` is ``CHOICE``; ``min``/``max`` bound ``INTEGER``/``PORT``.

    Validation of these constraints is the host's responsibility (``validate``);
    the dataclass only carries the declaration.
    """

    key: str
    kind: QuestionKind
    prompt: str
    default: str | None = None
    choices: tuple[str, ...] | None = None
    min: int | None = None
    max: int | None = None

    def validate(self) -> str | None:
        """Return a reason string if the declaration is malformed, else ``None``.

        Kept as data-only structural validation (no host access): choice needs
        choices, non-choice must not carry them, bounds only apply to numeric
        kinds, and a low>high bound is rejected.
        """
        if not self.key or not self.prompt:
            return "question requires a non-empty key and prompt"
        if self.kind is QuestionKind.CHOICE:
            if not self.choices:
                return f"choice question {self.key!r} requires choices"
        elif self.choices is not None:
            return f"question {self.key!r} of kind {self.kind.value} must not carry choices"
        numeric = self.kind in (QuestionKind.INTEGER, QuestionKind.PORT)
        if not numeric and (self.min is not None or self.max is not None):
            return f"question {self.key!r} of kind {self.kind.value} must not carry min/max"
        if self.min is not None and self.max is not None and self.min > self.max:
            return f"question {self.key!r} has min > max"
        return None


@dataclass(frozen=True, slots=True)
class Answer:
    """An operator's answer to one question. For a ``SECRET_REF`` question,
    ``value`` is a reference id the host resolves, never the secret itself."""

    key: str
    value: str


# An answer set keyed by question key. Mutable at the boundary where the host
# assembles it; wizards receive it read-through and never write host state.
Answers = dict[str, Answer]


def answers_from(pairs: list[Answer]) -> Answers:
    """Build an ``Answers`` map from a list, rejecting duplicate keys."""
    out: Answers = {}
    for answer in pairs:
        if answer.key in out:
            raise ValueError(f"duplicate answer key {answer.key!r}")
        out[answer.key] = answer
    return out
