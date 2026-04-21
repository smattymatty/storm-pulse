"""Coordinate reading, parsing, batching, and shipping for one log group."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from stormpulse.config import LogGroupConfig
from stormpulse.logging.parsers import PARSERS
from stormpulse.logging.tailer import DockerTailer, LogTailer, StreamingDockerTailer

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Batch:
    """A batch of parsed log entries ready to ship."""

    lines: list[dict[str, Any]]
    dropped: int
    from_position: int | str
    to_position: int | str


class LogShipper:
    """Pull lines from a tailer, filter, parse, and batch for shipping."""

    def __init__(self, group: LogGroupConfig, tailer: LogTailer | DockerTailer | StreamingDockerTailer) -> None:
        self._group = group
        self._tailer = tailer
        parser = PARSERS.get(group.parser)
        if parser is None:
            raise ValueError(f"Unknown parser: {group.parser!r}")
        self._parser: Callable[[str], dict[str, Any] | None] = parser

    @property
    def group_name(self) -> str:
        return self._group.name

    @property
    def parser_name(self) -> str:
        return self._group.parser

    @property
    def tailer(self) -> LogTailer | DockerTailer | StreamingDockerTailer:
        return self._tailer

    @property
    def ship_interval_seconds(self) -> float:
        return self._group.ship_interval_seconds

    def collect_batch(self) -> Batch | None:
        """Read new lines, filter, parse, and return a batch ready to ship.

        Returns ``None`` when nothing ship-worthy was produced:
        - No new lines on disk this interval.
        - All new lines were filtered out (not relevant to this group).

        Returns a Batch with ``lines=[]`` and ``dropped > 0`` when lines
        were read but all failed to parse — the dashboard needs this signal
        to detect a source producing unparseable output.

        Caller is responsible for invoking this from a thread.
        """
        max_batch = self._group.max_lines_per_batch
        raw_lines, from_pos, to_pos = self._tailer.read_new_lines(max_batch)
        if not raw_lines:
            return None

        filter_substr = self._group.filter_contains
        parsed: list[dict[str, Any]] = []
        dropped = 0
        any_matched_filter = False

        for line in raw_lines:
            if filter_substr and filter_substr not in line:
                # Silent skip — not a drop; just not relevant to this group.
                continue
            any_matched_filter = True
            entry = self._parser(line)
            if entry is None:
                dropped += 1
                continue
            parsed.append(entry)

        # Nothing matched the filter — not our business, nothing to ship.
        if filter_substr and not any_matched_filter:
            return None

        return Batch(
            lines=parsed,
            dropped=dropped,
            from_position=from_pos,
            to_position=to_pos,
        )
