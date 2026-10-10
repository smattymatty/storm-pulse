"""Fixtures shared by the protocol tests: one frame per message type, and the
fully populated instance builder the codec and ``make`` tests both walk with.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields, is_dataclass
from enum import StrEnum
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

import pytest

from stormpulse.protocol import SPEAKS
from tests.helpers import register_payload


@pytest.fixture
def heartbeat_dict() -> dict[str, Any]:
    return {
        "v": SPEAKS[-1],
        "type": "heartbeat",
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "ts": "2026-02-21T12:00:00Z",
        "agent_id": "vps-toronto-01",
        "payload": {},
    }


@pytest.fixture
def metrics_dict() -> dict[str, Any]:
    return {
        "v": SPEAKS[-1],
        "type": "metrics.push",
        "id": "550e8400-e29b-41d4-a716-446655440001",
        "ts": "2026-02-21T12:00:00Z",
        "agent_id": "vps-toronto-01",
        "payload": {
            "cpu_percent": 23.5,
            "memory_percent": 61.2,
            "memory_used_mb": 1245.0,
            "memory_total_mb": 2048.0,
            "disk_percent": 45.0,
            "disk_used_gb": 18.2,
            "disk_total_gb": 40.0,
            "load_avg_1m": 0.75,
            "load_avg_5m": 0.50,
            "uptime_seconds": 864000.0,
            "containers": [
                {"name": "web", "status": "running", "image": "myapp:latest"},
            ],
        },
    }


@pytest.fixture
def command_request_dict() -> dict[str, Any]:
    return {
        "v": SPEAKS[-1],
        "type": "command.request",
        "id": "550e8400-e29b-41d4-a716-446655440002",
        "ts": "2026-02-21T12:00:00Z",
        "agent_id": "vps-toronto-01",
        "payload": {
            "command": "git_pull",
            "params": {},
            "hmac": "abc123",
            "nonce": "nonce-1",
        },
    }


@pytest.fixture
def command_result_dict() -> dict[str, Any]:
    return {
        "v": SPEAKS[-1],
        "type": "command.result",
        "id": "550e8400-e29b-41d4-a716-446655440003",
        "ts": "2026-02-21T12:00:00Z",
        "agent_id": "vps-toronto-01",
        "payload": {
            "request_id": "550e8400-e29b-41d4-a716-446655440002",
            "command": "git_pull",
            "group": "deploy",
            "success": True,
            "exit_code": 0,
            "stdout": "Already up to date.\n",
            "stderr": "",
            "duration_ms": 342,
        },
    }


@pytest.fixture
def command_sequence_dict() -> dict[str, Any]:
    return {
        "v": SPEAKS[-1],
        "type": "command.sequence",
        "id": "550e8400-e29b-41d4-a716-446655440004",
        "ts": "2026-02-21T12:00:00Z",
        "agent_id": "vps-toronto-01",
        "payload": {
            "sequence_id": "seq-001",
            "steps": [
                {"command": "git_pull", "params": {}},
                {"command": "docker_logs", "params": {"tail": "100"}},
            ],
            "stop_on_failure": True,
            "hmac": "def456",
            "nonce": "nonce-2",
        },
    }


@pytest.fixture
def register_dict() -> dict[str, Any]:
    return {
        "v": SPEAKS[-1],
        "type": "register",
        "id": "550e8400-e29b-41d4-a716-446655440005",
        "ts": "2026-02-21T12:00:00Z",
        "agent_id": "vps-toronto-01",
        "payload": register_payload(),
    }


def _populated(annotation: Any) -> Any:
    """A fully populated value for a wire annotation: every optional present,
    a union taking its first non-null arm, every nested dataclass built."""
    origin, args = get_origin(annotation), get_args(annotation)
    if origin in (UnionType, Union):
        return _populated(next(a for a in args if a is not type(None)))
    if origin is list:
        return [_populated(args[0])]
    if origin is dict:
        return {"k": _populated(args[1])}
    if is_dataclass(annotation) and isinstance(annotation, type):
        hints = get_type_hints(annotation)
        return annotation(
            **{f.name: _populated(hints[f.name]) for f in fields(annotation)}
        )
    if annotation is Any:
        return {"any": 1}
    if issubclass(annotation, StrEnum):
        return next(iter(annotation))
    return {bool: False, int: 7, float: 1.5, str: "s"}[annotation]


@pytest.fixture
def populated() -> Callable[[Any], Any]:
    return _populated
