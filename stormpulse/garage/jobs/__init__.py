"""Garage's long-running job handlers, one module per ``mode="job"`` command;
each exports a ``make_*_handler`` factory that ``garage/commands/jobs.py`` consumes."""

from enum import StrEnum


class RollbackStatus(StrEnum):
    """How far a failed job's reverse-order cleanup got; ``rollback_status`` on the wire."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    NOT_REQUIRED = "not_required"
