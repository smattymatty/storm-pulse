"""Structured JSON logger for Storm Pulse's own activity log.

Writes one JSON object per line to /var/log/stormpulse/agent.log.
Runs ALONGSIDE stdlib logging — stdlib goes to stderr/journald,
this class ships structured events to disk for tailing and shipping
back to the dashboard.

Sensitive command output is NEVER written. ``log_command_result``
omits stdout/stderr when ``sensitive=True``.
"""

from __future__ import annotations

import fcntl
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class PulseLogger:
    """Append structured JSON lines to a log file, thread- and process-safe."""

    def __init__(self, log_path: Path, agent_id: str) -> None:
        self._path = log_path
        self._agent_id = agent_id
        self._lock = threading.Lock()

    def _write(
        self,
        level: str,
        message: str,
        event_type: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "level": level,
            "message": message,
            "event_type": event_type,
            "agent_id": self._agent_id,
            "detail": detail or {},
        }
        line = json.dumps(entry, separators=(",", ":")) + "\n"

        with self._lock:
            try:
                with open(self._path, "a", encoding="utf-8") as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                    try:
                        f.write(line)
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except OSError as exc:
                # Fall back to stdlib logger so we never lose the event entirely.
                logger.warning("Failed writing pulse log to %s: %s", self._path, exc)

    def info(
        self, message: str, event_type: str, detail: dict[str, Any] | None = None,
    ) -> None:
        self._write("INFO", message, event_type, detail)

    def warning(
        self, message: str, event_type: str, detail: dict[str, Any] | None = None,
    ) -> None:
        self._write("WARNING", message, event_type, detail)

    def error(
        self, message: str, event_type: str, detail: dict[str, Any] | None = None,
    ) -> None:
        self._write("ERROR", message, event_type, detail)

    def log_command_result(
        self,
        command: str,
        success: bool,
        duration_ms: int,
        sensitive: bool,
        sequence_id: str | None = None,
    ) -> None:
        """Log a command execution result.

        When ``sensitive=True``, ONLY the command name, success flag,
        and duration are recorded. stdout/stderr are never written here
        regardless of this flag — the caller is responsible for never
        passing command output into a PulseLogger call.
        """
        detail: dict[str, Any] = {
            "command": command,
            "success": success,
            "duration_ms": duration_ms,
            "sensitive": sensitive,
        }
        if sequence_id is not None:
            detail["sequence_id"] = sequence_id
        level = "INFO" if success else "WARNING"
        self._write(level, f"Command {command} {'succeeded' if success else 'failed'}",
                    "command", detail)
