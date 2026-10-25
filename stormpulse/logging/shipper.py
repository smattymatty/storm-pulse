"""Coordinate reading, parsing, batching, and shipping for one log group."""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any

from stormpulse.config import LogGroupConfig
from stormpulse.logging.parsers import PARSERS
from stormpulse.logging.receipt import Receipt, holds_back
from stormpulse.logging.tailer import (
    DockerTailer,
    JournaldTailer,
    LogTailer,
    StreamingDockerTailer,
)

logger = logging.getLogger(__name__)

# A ``(key_id, bucket-name) -> bucket_id`` lookup. Typed as a bare callable so
# the logging layer stays free of any garage-layer dependency (the four-layer
# topology forbids logging from importing garage); the agent layer builds the
# concrete resolver and passes it in.
BucketIdResolver = Callable[[str, str], str]

# Only this parser's lines carry the key_id the resolver needs beside the
# bucket name; other parsers leave bucket_id off the wire entirely.
_BUCKET_ID_PARSER = "garage_s3"


@dataclass(frozen=True, slots=True)
class Batch:
    """A batch of parsed log entries ready to ship."""

    lines: list[dict[str, Any]]
    dropped: int
    receipt: Receipt

    @property
    def from_position(self) -> int | str:
        return self.receipt.from_position

    @property
    def to_position(self) -> int | str:
        return self.receipt.to_position


class LogShipper:
    """Pull lines from a tailer, filter, parse, and batch for shipping."""

    def __init__(
        self,
        group: LogGroupConfig,
        tailer: LogTailer | DockerTailer | StreamingDockerTailer | JournaldTailer,
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
    def tailer(
        self,
    ) -> LogTailer | DockerTailer | StreamingDockerTailer | JournaldTailer:
        return self._tailer

    @property
    def ship_interval_seconds(self) -> float:
        return self._group.ship_interval_seconds

    def collect_batch(
        self,
        bucket_id_resolver: BucketIdResolver | None = None,
        *,
        pending: Collection[Receipt] | None = None,
    ) -> Batch | None:
        """Read, filter and parse new lines from a worker thread.

        Return None when no lines match, or an empty batch with a drop count
        when every matching line fails parsing. The current-state resolver maps
        (key_id, bucket name) to bucket_id for garage_s3; unresolved IDs are empty.
        ``pending`` is every receipt still awaiting its ACK; see ``_skip_filtered``.
        """
        raw_lines, receipt = self._tailer.read_new_lines(
            self._group.max_lines_per_batch
        )
        if not raw_lines:
            return None
        filter_substr = self._group.filter_contains
        matched = [line for line in raw_lines if filter_substr in line]
        # Nothing matched the filter - not our business, nothing to ship.
        if not matched:
            self._skip_filtered(receipt, pending)
            return None
        parsed, dropped = self._parse(matched, bucket_id_resolver)
        return Batch(lines=parsed, dropped=dropped, receipt=receipt)

    def _parse(
        self, lines: list[str], bucket_id_resolver: BucketIdResolver | None
    ) -> tuple[list[dict[str, Any]], int]:
        """Parse matched lines; return the entries and how many failed to parse."""
        stamp_bucket_id = self._group.parser == _BUCKET_ID_PARSER
        parsed: list[dict[str, Any]] = []
        for line in lines:
            entry = self._parser(line)
            if entry is None:
                continue
            if stamp_bucket_id:
                entry["bucket_id"] = (
                    bucket_id_resolver(entry.get("key_id", ""), entry.get("bucket", ""))
                    if bucket_id_resolver is not None
                    else ""
                )
            parsed.append(entry)
        return parsed, len(lines) - len(parsed)

    def _skip_filtered(
        self, receipt: Receipt, pending: Collection[Receipt] | None
    ) -> None:
        """STAB-01: checkpoint a wholly filtered read locally; no wire, no empty batch.

        Held while ``pending`` is unknown (None) or any receipt in it may carry
        matches below this read's end: their ACK, not this, moves past them.
        """
        if pending is None or any(holds_back(p, receipt) for p in pending):
            return
        if self._tailer.confirm_shipped(receipt):
            logger.debug(
                "Filtered range consumed for group %s to %s",
                self._group.name,
                receipt.to_position,
            )

    def confirm(self, receipt: Receipt) -> bool:
        """Hand an ACKed receipt to this group's own tailer; False when it refuses."""
        if self._tailer.confirm_shipped(receipt):
            return True
        logger.info(
            "Refused log receipt for group %s (issued for %s at %s)",
            self._group.name,
            receipt.group,
            receipt.source,
        )
        return False
