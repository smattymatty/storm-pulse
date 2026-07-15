"""Wizard engine errors (P2, CORE-007 decision 5)."""

from __future__ import annotations


class WizardError(Exception):
    """A plan is invalid or a mutation could not be applied/verified. Triggers a
    reverse-order rollback of the steps already applied."""


class CompensationError(Exception):
    """A compensation (inverse) itself failed during rollback. This is surfaced
    loudly and turns the receipt status into ``partial_rollback`` (I5); it never
    silently aborts the remaining compensations."""
