"""Coordinate reading, parsing, batching, and shipping for one log group."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from stormpulse.config import LogGroupConfig
from stormpulse.logging.parsers import PARSERS
from stormpulse.logging.tailer import DockerTailer, LogTailer, StreamingDockerTailer

logger = logging.getLogger(__name__)

# A ``(key_id, bucket-name) -> bucket_id`` lookup. Typed as a bare callable so
# the logging layer stays free of any garage-layer dependency (the four-layer
# topology forbids logging from importing garage); the agent layer builds the
# concrete resolver and passes it in.
BucketIdResolver = Callable[[str, str], str]

# Only this parser's lines carry the (key_id, bucket-name) pair the resolver
# needs; other parsers leave the field off the wire entirely.
_BUCKET_ID_PARSER = "garage_s3"


@dataclass(frozen=True, slots=True)
class Batch:
    """A batch of parsed log entries ready to ship."""

    lines: list[dict[str, Any]]
    dropped: int
    from_position: int | str
    to_position: int | str


class LogShipper:
    """Pull lines from a tailer, filter, parse, and batch for shipping."""

    def __init__(
        self,
        group: LogGroupConfig,
        tailer: LogTailer | DockerTailer | StreamingDockerTailer,
    ) -> None:
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

    def collect_batch(
        self, bucket_id_resolver: BucketIdResolver | None = None,
    ) -> Batch | None:
        """Read new lines, filter, parse, and return a batch ready to ship.

        Returns ``None`` when nothing ship-worthy was produced:
        - No new lines on disk this interval.
        - All new lines were filtered out (not relevant to this group).

        Returns a Batch with ``lines=[]`` and ``dropped > 0`` when lines
        were read but all failed to parse - the dashboard needs this signal
        to detect a source producing unparseable output.

        ``bucket_id_resolver`` is the tick-fresh ``(key_id, name) -> bucket_id``
        map. When supplied for a ``garage_s3`` group, every parsed
        line gets a ``bucket_id`` field (``''`` when the bucket is not in the
        last Garage-state snapshot). Other groups ignore it: their lines carry
        no bucket name and the website never reads the field for them.

        Caller is responsible for invoking this from a thread.
        """
        max_batch = self._group.max_lines_per_batch
        raw_lines, from_pos, to_pos = self._tailer.read_new_lines(max_batch)
        if not raw_lines:
            return None

        filter_substr = self._group.filter_contains
        stamp_bucket_id = (
            bucket_id_resolver is not None
            and self._group.parser == _BUCKET_ID_PARSER
        )
        parsed: list[dict[str, Any]] = []
        dropped = 0
        any_matched_filter = False

        for line in raw_lines:
            if filter_substr and filter_substr not in line:
                # Silent skip - not a drop; just not relevant to this group.
                continue
            any_matched_filter = True
            entry = self._parser(line)
            if entry is None:
                dropped += 1
                continue
            if stamp_bucket_id:
                assert bucket_id_resolver is not None  # narrowed by stamp_bucket_id
                entry["bucket_id"] = bucket_id_resolver(
                    entry.get("key_id", ""), entry.get("bucket", ""),
                )
            parsed.append(entry)

        # Nothing matched the filter - not our business, nothing to ship.
        if filter_substr and not any_matched_filter:
            return None

        return Batch(
            lines=parsed,
            dropped=dropped,
            from_position=from_pos,
            to_position=to_pos,
        )
