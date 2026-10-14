"""Helpers shared by the ``test_event_spool_*`` files: a declared source and
the segments an emitter writes into its spool."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from stormpulse import events
from stormpulse.agent.external_adapters import _event_source
from stormpulse.sdk import SdkEventSource

SOURCE = "probe_gate"
NOBODY = 65534


def declared(spool: Path, name: str = SOURCE) -> events.EventSource:
    sdk = SdkEventSource(
        source=name,
        kinds={"request_seen": ("record_id", "status", "duration_ms")},
        spool_dir=str(spool),
    )
    source = _event_source(sdk)
    assert source is not None
    return source


def runtime(status: str, source: events.EventSource | None) -> Any:
    """An IntegrationRuntime carrying only what ``spool_feeders`` reads."""
    return SimpleNamespace(
        status=status, descriptor=SimpleNamespace(event_source=source)
    )


def segment(spool: Path, name: str, *records: dict[str, Any], schema: int = 1) -> Path:
    lines = [json.dumps({"schema": schema}), *(json.dumps(r) for r in records)]
    tmp = spool / f".{name}.tmp"
    tmp.write_text("\n".join(lines) + "\n")
    path = spool / name
    os.rename(tmp, path)
    return path


def seen(record_id: str, **extra: Any) -> dict[str, Any]:
    return {"kind": "request_seen", "record_id": record_id, "status": 200, **extra}
