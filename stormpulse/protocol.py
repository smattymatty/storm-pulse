"""Wire protocol: message types, envelope, serialization."""

from __future__ import annotations

import json
import uuid
from dataclasses import MISSING, asdict, dataclass, fields
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Self


class ProtocolError(Exception):
    """Raised when a message fails validation or parsing."""


class MessageType(StrEnum):
    """All valid message types in the Storm Pulse protocol v1."""

    # Agent → Dashboard
    HEARTBEAT = "heartbeat"
    METRICS_PUSH = "metrics.push"
    COMMAND_RESULT = "command.result"
    COMMAND_PROGRESS = "command.progress"
    REGISTER = "register"
    LOG_BATCH = "log.batch"
    SIGNOFF_STATE = "signoff.state"

    # Dashboard → Agent (actionable)
    COMMAND_REQUEST = "command.request"
    COMMAND_SEQUENCE = "command.sequence"
    LOG_BATCH_ACK = "log.batch.ack"

    # Dashboard → Agent (acknowledgements)
    REGISTER_OK = "register.ok"
    HEARTBEAT_ACK = "heartbeat.ack"
    METRICS_ACK = "metrics.ack"
    COMMAND_RESULT_ACK = "command.result.ack"
    SIGNOFF_STATE_ACK = "signoff.state.ack"
    ERROR = "error"


def _payload_from_dict[T](
    cls: type[T], data: Any, *, nested: dict[str, type] | None = None
) -> T:
    """Validate required fields and construct a payload dataclass.

    Args:
        cls: The target dataclass type.
        data: Raw dict from JSON.
        nested: Map of field name -> element type for list fields that need
                recursive deserialization (e.g. {"containers": ContainerInfo}).
    """
    if not isinstance(data, dict):
        raise ProtocolError(f"{cls.__name__}: expected dict, got {type(data).__name__}")

    cls_fields = fields(cls)  # type: ignore[arg-type]
    required = {
        f.name
        for f in cls_fields
        if f.default is MISSING and f.default_factory is MISSING
    }

    missing_fields = required - data.keys()
    if missing_fields:
        raise ProtocolError(f"{cls.__name__}: missing fields: {missing_fields}")

    filtered: dict[str, Any] = {}
    for f in cls_fields:
        if f.name in data:
            val = data[f.name]
            if nested and f.name in nested:
                if not isinstance(val, list):
                    raise ProtocolError(
                        f"{cls.__name__}.{f.name}: expected list, got {type(val).__name__}"
                    )
                val = [_payload_from_dict(nested[f.name], item) for item in val]
            filtered[f.name] = val

    return cls(**filtered)


@dataclass(frozen=True, slots=True)
class ContainerInfo:
    """A single container's status."""

    name: str
    status: str
    image: str

    @classmethod
    def from_dict(cls, data: Any) -> Self:
        return _payload_from_dict(cls, data)


@dataclass(frozen=True, slots=True)
class MetricsPayload:
    """Payload for metrics.push messages."""

    cpu_percent: float
    memory_percent: float
    memory_used_mb: float
    memory_total_mb: float
    disk_percent: float
    disk_used_gb: float
    disk_total_gb: float
    load_avg_1m: float
    load_avg_5m: float
    uptime_seconds: float
    containers: list[ContainerInfo]
    # CORE-005: per-Integration reports keyed by id (status/disabled_reason/state),
    # replacing the old top-level ``garage`` key. Opaque to the protocol.
    integrations: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, data: Any) -> Self:
        return _payload_from_dict(cls, data, nested={"containers": ContainerInfo})


@dataclass(frozen=True, slots=True)
class CommandRequestPayload:
    """Payload for command.request (dashboard -> agent)."""

    command: str
    params: dict[str, str]
    hmac: str
    nonce: str

    @classmethod
    def from_dict(cls, data: Any) -> Self:
        return _payload_from_dict(cls, data)


@dataclass(frozen=True, slots=True)
class CommandSequencePayload:
    """Payload for command.sequence (dashboard -> agent)."""

    sequence_id: str
    commands: list[str]
    stop_on_failure: bool
    hmac: str
    nonce: str

    @classmethod
    def from_dict(cls, data: Any) -> Self:
        return _payload_from_dict(cls, data)


@dataclass(frozen=True, slots=True)
class CommandResultPayload:
    """Payload for command.result (agent -> dashboard)."""

    request_id: str
    command: str
    group: str
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    sequence_id: str | None = None
    failure_reason: str | None = None

    @classmethod
    def from_dict(cls, data: Any) -> Self:
        return _payload_from_dict(cls, data)


@dataclass(frozen=True, slots=True)
class CommandProgressPayload:
    """Payload for command.progress (agent -> dashboard).

    Long-running commands emit one or more of these between the originating
    command.request and the terminal command.result. The first event always
    has stage="starting"; the terminal command.result still closes the job.
    """

    request_id: str
    command: str
    group: str
    stage: str
    current: int
    total: int | None = None
    message: str = ""

    @classmethod
    def from_dict(cls, data: Any) -> Self:
        return _payload_from_dict(cls, data)


@dataclass(frozen=True, slots=True)
class RegisterPayload:
    """Payload for register messages."""

    version: str
    pulse_token: str
    commands: dict[str, Any] | None = None
    # CORE-005: per-Integration reports keyed by id, replacing top-level ``garage``.
    integrations: dict[str, Any] | None = None
    log_groups: list[str] | None = None
    system_inventory: dict[str, Any] | None = None
    # Whether the dashboard's verify-block hatch is currently disabled.
    # See stormpulse.signoff and ADR CORE-004. None on agents that
    # predate the seal feature.
    signoff_sealed: bool | None = None
    # ISO-8601 UTC timestamp at which the agent transitioned to
    # unsealed. None when sealed, when the agent predates the marker,
    # or when an operator removed the seal file by hand without going
    # through the CLI. Dashboard uses this for "unsealed for X" displays
    # and for the "unsealed > N hours" pager trigger - the agent owns
    # the authoritative wall-clock so the dashboard doesn't have to
    # guess from its own register history.
    unsealed_since: str | None = None

    @classmethod
    def from_dict(cls, data: Any) -> Self:
        return _payload_from_dict(cls, data)


@dataclass(frozen=True, slots=True)
class SignoffStatePayload:
    """Payload for signoff.state messages (agent -> dashboard).

    Mid-session push of the verify-block seal state. The register
    payload still carries the at-connect snapshot; this envelope
    exists so the dashboard does not lag a CLI-driven transition
    while a connection is up. See ADR CORE-004 "Live propagation"
    and the spec's `signoff.state` section.
    """

    signoff_sealed: bool
    unsealed_since: str | None = None

    @classmethod
    def from_dict(cls, data: Any) -> Self:
        return _payload_from_dict(cls, data)


@dataclass(frozen=True, slots=True)
class LogBatchPayload:
    """Payload for log.batch messages (agent -> dashboard)."""

    group: str
    parser: str
    batch_id: str
    lines: list[dict[str, Any]]
    dropped: int
    from_position: int | str
    to_position: int | str

    @classmethod
    def from_dict(cls, data: Any) -> Self:
        return _payload_from_dict(cls, data)


def _parse_timestamp(raw: Any) -> datetime:
    """Parse an ISO 8601 timestamp string, requiring timezone info."""
    if not isinstance(raw, str):
        raise ProtocolError(f"Timestamp must be a string, got {type(raw).__name__}")
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtocolError(f"Invalid timestamp: {raw!r}") from exc
    if ts.tzinfo is None:
        raise ProtocolError(f"Timestamp must include timezone: {raw!r}")
    return ts


def format_timestamp(ts: datetime) -> str:
    """Format a datetime to ISO 8601 with Z suffix for UTC."""
    return ts.isoformat().replace("+00:00", "Z")


_ENVELOPE_FIELDS = {"v", "type", "id", "ts", "agent_id", "payload"}


@dataclass(frozen=True, slots=True)
class Envelope:
    """The universal message envelope.

    Every message on the wire is an Envelope serialized to JSON.
    The payload is stored as a raw dict - consuming code parses it
    into typed payload dataclasses after matching on ``type``.
    """

    v: int
    type: MessageType
    id: str
    ts: datetime
    agent_id: str
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        if self.v != 1:
            raise ProtocolError(f"Unsupported protocol version: {self.v}")
        if not isinstance(self.type, MessageType):
            raise ProtocolError(f"Invalid message type: {self.type!r}")
        if not self.agent_id:
            raise ProtocolError("agent_id must not be empty")
        if self.ts.tzinfo is None:
            raise ProtocolError("Timestamp must be timezone-aware")

    @classmethod
    def from_json(cls, raw: str | bytes) -> Self:
        """Deserialize a JSON string into a validated Envelope.

        Validates envelope structure only. Payload is kept as a raw dict.
        Raises ProtocolError on any envelope-level failure.
        """
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ProtocolError(f"Invalid JSON: {exc}") from exc

        if not isinstance(data, dict):
            raise ProtocolError(f"Expected JSON object, got {type(data).__name__}")

        missing = _ENVELOPE_FIELDS - data.keys()
        if missing:
            raise ProtocolError(f"Missing envelope fields: {missing}")

        v = data["v"]
        if v != 1:
            raise ProtocolError(f"Unsupported protocol version: {v}")

        try:
            msg_type = MessageType(data["type"])
        except ValueError:
            raise ProtocolError(f"Unknown message type: {data['type']!r}")

        ts = _parse_timestamp(data["ts"])

        agent_id = data["agent_id"]
        if not isinstance(agent_id, str) or not agent_id:
            raise ProtocolError("agent_id must be a non-empty string")

        payload = data["payload"]
        if not isinstance(payload, dict):
            raise ProtocolError(f"payload must be a dict, got {type(payload).__name__}")

        return cls(
            v=1, type=msg_type, id=data["id"], ts=ts, agent_id=agent_id, payload=payload
        )

    def to_json(self) -> str:
        """Serialize this Envelope to compact JSON."""
        return json.dumps(self.to_dict(), separators=(",", ":"))

    def to_dict(self) -> dict[str, Any]:
        """Convert to a plain dict for inspection or serialization."""
        return {
            "v": self.v,
            "type": self.type.value,
            "id": self.id,
            "ts": format_timestamp(self.ts),
            "agent_id": self.agent_id,
            "payload": self.payload,
        }


def _make_envelope(
    agent_id: str, msg_type: MessageType, payload: dict[str, Any]
) -> Envelope:
    """Internal helper to build an Envelope with fresh id and timestamp."""
    return Envelope(
        v=1,
        type=msg_type,
        id=str(uuid.uuid4()),
        ts=datetime.now(UTC),
        agent_id=agent_id,
        payload=payload,
    )


def make_heartbeat(agent_id: str) -> Envelope:
    """Create a heartbeat envelope."""
    return _make_envelope(agent_id, MessageType.HEARTBEAT, {})


def make_register(
    agent_id: str,
    version: str,
    pulse_token: str,
    commands: dict[str, Any] | None = None,
    integrations: dict[str, Any] | None = None,
    log_groups: list[str] | None = None,
    system_inventory: dict[str, Any] | None = None,
    signoff_sealed: bool | None = None,
    unsealed_since: str | None = None,
) -> Envelope:
    """Create a register envelope."""
    return _make_envelope(
        agent_id,
        MessageType.REGISTER,
        asdict(
            RegisterPayload(
                version=version,
                pulse_token=pulse_token,
                commands=commands,
                integrations=integrations,
                log_groups=log_groups,
                system_inventory=system_inventory,
                signoff_sealed=signoff_sealed,
                unsealed_since=unsealed_since,
            )
        ),
    )


def make_metrics_push(
    agent_id: str,
    metrics: MetricsPayload,
    integrations: dict[str, Any] | None = None,
    job_load: dict[str, int] | None = None,
) -> Envelope:
    """Create a metrics.push envelope.

    ``job_load`` is the agent-level job-queue snapshot (pending + running job
    counts, observability #3); when present it rides the push at top level under
    ``jobs``, the same way ``integrations`` does. Opaque to the protocol.
    """
    payload = asdict(metrics)
    if integrations is not None:
        payload["integrations"] = integrations
    if job_load is not None:
        payload["jobs"] = job_load
    return _make_envelope(agent_id, MessageType.METRICS_PUSH, payload)


def make_command_result(
    agent_id: str,
    result: CommandResultPayload,
    *,
    extras: dict[str, Any] | None = None,
) -> Envelope:
    """Create a command.result envelope.

    ``extras`` are command-specific summary fields that ride at the top
    level of the payload alongside the standard ``CommandResultPayload``
    fields. Used by long-running commands (e.g. ``garage_bucket_clear``)
    to deliver per-operation summary data - ``deleted_count``,
    ``failed_count``, ``errors`` - without inventing a new message type.
    Extras must not collide with standard ``CommandResultPayload`` field
    names; collisions overwrite the standard field.
    """
    payload = asdict(result)
    if extras:
        payload = payload | extras
    return _make_envelope(agent_id, MessageType.COMMAND_RESULT, payload)


def make_command_progress(agent_id: str, progress: CommandProgressPayload) -> Envelope:
    """Create a command.progress envelope."""
    return _make_envelope(agent_id, MessageType.COMMAND_PROGRESS, asdict(progress))


def make_signoff_state(
    agent_id: str,
    *,
    sealed: bool,
    unsealed_since: str | None = None,
) -> Envelope:
    """Create a signoff.state envelope.

    `unsealed_since` SHOULD be a UTC ISO-8601 string when ``sealed`` is
    ``False`` and ``None`` when ``sealed`` is ``True``. The dashboard
    clears the column when sealed and persists the wall-clock when
    unsealed; see ADR DEVELOPER-005.
    """
    return _make_envelope(
        agent_id,
        MessageType.SIGNOFF_STATE,
        asdict(
            SignoffStatePayload(
                signoff_sealed=sealed,
                unsealed_since=unsealed_since,
            )
        ),
    )


def make_log_batch(
    agent_id: str,
    group: str,
    parser: str,
    batch_id: str,
    lines: list[dict[str, Any]],
    dropped: int,
    from_position: int | str,
    to_position: int | str,
) -> Envelope:
    """Create a log.batch envelope."""
    return _make_envelope(
        agent_id,
        MessageType.LOG_BATCH,
        asdict(
            LogBatchPayload(
                group=group,
                parser=parser,
                batch_id=batch_id,
                lines=lines,
                dropped=dropped,
                from_position=from_position,
                to_position=to_position,
            )
        ),
    )
