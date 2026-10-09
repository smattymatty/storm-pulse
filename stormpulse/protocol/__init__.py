"""Wire protocol: message types, envelope, serialization.

A package: the catalogue, the envelope and the ``make_*`` factories live here;
``shape`` holds the reflection walkers, ``reasons`` the closed vocabularies.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import (
    MISSING,
    asdict,
    dataclass,
    field,
    fields,
    is_dataclass,
    replace,
)
from datetime import UTC, datetime
from enum import StrEnum
from types import UnionType
from typing import Any, Self, Union, get_args, get_origin, get_type_hints

from stormpulse.protocol.reasons import CancelReason as CancelReason
from stormpulse.protocol.reasons import FailureReason as FailureReason
from stormpulse.protocol.shape import dataclass_wire_shape as dataclass_wire_shape
from stormpulse.protocol.shape import dataclass_wire_types as dataclass_wire_types
from stormpulse.protocol.shape import wire_type_name

#: Envelope versions this agent speaks, oldest first; the last is what it
#: writes. Negotiated at register (CORE-011 d2). A frame outside it is refused.
SPEAKS: tuple[int, ...] = (2,)


class ProtocolError(Exception):
    """Raised when a message fails validation or parsing.

    ``reason`` is a closed enum naming why, the way ``AuthError`` does;
    the ``error`` reply quotes it, never the human message.
    """

    #: Every value ``reason`` may take. A refusal outside this set is a bug.
    REASONS = frozenset(  # skylos: ignore - documented closed enum; the contract, not a consumer
        {"unsupported_version", "unknown_type", "bad_envelope"}
    )

    def __init__(
        self, message: str, *, reason: str = "bad_envelope", msg_type: str | None = None
    ) -> None:
        super().__init__(message)
        self.reason = reason
        #: The refused frame's ``type`` when one was readable; the reply quotes it.
        self.msg_type = msg_type


def check_version(v: Any) -> int:
    """The one place a version meets ``SPEAKS``, an envelope's ``v`` or the one
    ``register.ok`` picked; bool is not an int here. The refusal names both sides."""
    if type(v) is not int or v not in SPEAKS:
        raise ProtocolError(
            f"Unsupported protocol version: {v!r}; this agent speaks {list(SPEAKS)}",
            reason="unsupported_version",
        )
    return v


class WirePayload:
    """Base of every payload class: one ``from_dict``, so a table can hold them all."""

    __slots__ = ()

    @classmethod
    def from_dict(cls, data: Any) -> Self:
        return _payload_from_dict(cls, data)


class MessageType(StrEnum):
    """All valid message types in the Storm Pulse protocol v1."""

    # Agent → Dashboard
    HEARTBEAT = "heartbeat"
    METRICS_PUSH = "metrics.push"
    COMMAND_RESULT = "command.result"
    COMMAND_PROGRESS = "command.progress"
    COMMAND_CANCELLED = "command.cancelled"
    REGISTER = "register"
    LOG_BATCH = "log.batch"
    EVENTS_BATCH = "events.batch"
    SIGNOFF_STATE = "signoff.state"

    # Dashboard → Agent (actionable)
    COMMAND_REQUEST = "command.request"
    COMMAND_SEQUENCE = "command.sequence"
    LOG_BATCH_ACK = "log.batch.ack"
    EVENTS_BATCH_ACK = "events.batch.ack"
    EVENTS_BATCH_NACK = "events.batch.nack"

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
    """Validate required fields and construct ``cls`` from ``data``. ``nested``
    maps field name -> element type for ``list[X]`` / ``dict[str, X]`` fields
    needing recursive deserialization (e.g. {"containers": ContainerInfo});
    a null stays null."""
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

    hints = get_type_hints(cls) if nested else {}
    filtered: dict[str, Any] = {}
    for f in cls_fields:
        if f.name in data:
            val = data[f.name]
            if nested and f.name in nested and val is not None:
                val = _nested_from_dict(cls, f.name, hints[f.name], nested[f.name], val)
            filtered[f.name] = val

    return cls(**filtered)


def _nested_from_dict(cls: type, name: str, hint: Any, item: type, val: Any) -> Any:
    """Deserialize one nested field by its annotation: a dataclass itself, a
    mapping's values or a list's items; the wrong JSON container is a ProtocolError."""
    members = (
        [a for a in get_args(hint) if a is not type(None)]
        if get_origin(hint) in (UnionType, Union)
        else [hint]
    )
    if any(is_dataclass(m) for m in members):
        return _payload_from_dict(item, val)
    if any(wire_type_name(m) == "object" for m in members):
        if not isinstance(val, dict):
            raise ProtocolError(
                f"{cls.__name__}.{name}: expected dict, got {type(val).__name__}"
            )
        return {k: _payload_from_dict(item, v) for k, v in val.items()}
    if not isinstance(val, list):
        raise ProtocolError(
            f"{cls.__name__}.{name}: expected list, got {type(val).__name__}"
        )
    return [_payload_from_dict(item, v) for v in val]


@dataclass(frozen=True, slots=True)
class ContainerInfo(WirePayload):
    """A single container's status."""

    name: str
    status: str
    image: str


@dataclass(frozen=True, slots=True)
class IntegrationReport(WirePayload):
    """One ``integrations.<id>`` entry (CORE-005): wire status and why it is
    disabled, the state blob (its shape declared under ``integrations`` in the
    artifact; null unless live), the command receipt (CORE-011 d4): the digest
    a grant pins, ``""`` with no commands, and the fence reason or null; and
    how complete and fresh the snapshot is (CORE-011 d5), ``generation`` through
    ``changed_ids``. Still a full snapshot, never a partial."""

    status: str
    disabled_reason: str | None
    state: dict[str, Any] | None
    command_specs_digest: str
    commands_fenced: str | None
    generation: int
    complete: bool
    last_full_read_at: str
    changed_ids: list[str]


@dataclass(frozen=True, slots=True)
class MetricsPayload(WirePayload):
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
    # CORE-005: per-Integration reports keyed by id, replacing the old top-level
    # ``garage`` key.
    integrations: dict[str, IntegrationReport] | None = None
    # The job-queue snapshot (pending + running counts); a real field so the wire
    # contract declares it (CORE-011 d3). Absent on the wire when None, never null.
    jobs: dict[str, int] | None = None

    @classmethod
    def from_dict(cls, data: Any) -> Self:
        return _payload_from_dict(
            cls,
            data,
            nested={"containers": ContainerInfo, "integrations": IntegrationReport},
        )


@dataclass(frozen=True, slots=True)
class CommandRequestPayload(WirePayload):
    """Payload for command.request (dashboard -> agent)."""

    command: str
    params: dict[str, Any]
    hmac: str
    nonce: str


@dataclass(frozen=True, slots=True)
class Step(WirePayload):
    """One step of a command.sequence: a subprocess command and its params
    (``{}`` when it takes none), validated like a single request's."""

    command: str
    params: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CommandSequencePayload(WirePayload):
    """Payload for command.sequence (dashboard -> agent). Each step's result
    carries this ``sequence_id`` and its ``step_index`` (CORE-011 d9)."""

    sequence_id: str
    steps: list[Step]
    stop_on_failure: bool
    hmac: str
    nonce: str

    @classmethod
    def from_dict(cls, data: Any) -> Self:
        return _payload_from_dict(cls, data, nested={"steps": Step})


@dataclass(frozen=True, slots=True)
class CommandCancelledPayload(WirePayload):
    """Payload for command.cancelled (agent -> dashboard): the terminal word for
    a job that will send no ``command.result``, one per in-flight job on a
    graceful stop while the socket is still open (CORE-011 d9)."""

    request_id: str
    command: str
    reason: CancelReason


@dataclass(frozen=True, slots=True)
class ResultError(WirePayload):
    """A command.result's ``error`` (CORE-011 d9): ``code`` is the agent's word
    (the failure reason unless a handler had a finer one); ``http_status`` and
    ``upstream_code`` are what the upstream answered when a job parsed one;
    ``message`` is the human line (stderr). Not ``commands.CommandError``, the
    whitelist's exception."""

    code: str
    http_status: int | None
    upstream_code: str | None
    message: str


@dataclass(frozen=True, slots=True)
class CommandResultPayload(WirePayload):
    """Payload for command.result (agent -> dashboard).

    ``result`` holds the command's own figures, nested so no key of theirs can
    shadow a standard field (CORE-011 d9); its shape per command is
    ``CommandSpec.result``. ``error`` rides on every failure, never a success:
    a failure built without one gets the reason and stderr as its ``ResultError``.
    """

    request_id: str
    command: str
    group: str
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    sequence_id: str | None = None
    failure_reason: FailureReason | None = None
    result: dict[str, Any] | None = None
    error: ResultError | None = None
    # The step's position in its sequence, 0-based; null outside one.
    step_index: int | None = None

    def __post_init__(self) -> None:
        # The one rule for producers, ``from_dict`` and tests alike: a member's
        # string is lifted, a stranger refused, a failure's error derived.
        if self.failure_reason is not None:
            try:
                reason = FailureReason(self.failure_reason)
            except ValueError:
                raise ValueError(
                    f"{self.command}: failure_reason {self.failure_reason!r} is not a FailureReason"
                ) from None
            object.__setattr__(self, "failure_reason", reason)
        if self.success and self.error is not None:
            raise ValueError(
                f"{self.command}: a command.result cannot succeed with an error"
            )
        if not self.success and self.error is None:
            derived = ResultError(
                code=self.failure_reason or "unspecified",
                http_status=None,
                upstream_code=None,
                message=self.stderr,
            )
            object.__setattr__(self, "error", derived)

    @classmethod
    def from_dict(cls, data: Any) -> Self:
        return _payload_from_dict(cls, data, nested={"error": ResultError})


@dataclass(frozen=True, slots=True)
class TransferStats:
    """Structured telemetry for a job that moves bytes (today: rclone).

    One value, not four loose optionals: a transfer job always knows its rate
    and counts, a non-transfer job never does. ``eta_seconds`` alone is optional
    within a transfer (rclone reports null until it has samples). Aggregates
    only, never a filename (tests/rclone/test_migrate.py holds this line).
    """

    rate_bytes_per_sec: int
    objects_current: int
    objects_total: int
    eta_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class CommandProgressPayload(WirePayload):
    """Payload for command.progress (agent -> dashboard).

    Long-running commands emit one or more between command.request and the
    terminal command.result; the first has stage="starting". The transfer
    fields are optional and flat on the wire (the agent carries them as a
    ``TransferStats``), absent on every non-transfer command.
    """

    request_id: str
    command: str
    group: str
    stage: str
    current: int
    total: int | None = None
    message: str = ""
    rate_bytes_per_sec: int | None = None
    eta_seconds: int | None = None
    objects_current: int | None = None
    objects_total: int | None = None
    # Bytes freed so far by a drain job (garage_bucket_clear). Flat and
    # optional like the transfer fields: absent on every other command,
    # dropped tolerantly by an old dashboard, no protocol bump.
    bytes_freed: int | None = None


@dataclass(frozen=True, slots=True)
class Component(WirePayload):
    """One versioned piece of the node a register declares (CORE-011 d7): the
    agent itself and whatever each integration contributes. ``digest`` is ``""``
    where the component has none."""

    name: str
    version: str
    digest: str


@dataclass(frozen=True, slots=True)
class RegisterPayload(WirePayload):
    """Payload for ``register``: the node's whole self-declaration (CORE-011 d7).

    Every field is present on every register; empty is ``{}``, ``[]`` or ``""``,
    never absent. Only ``unsealed_since`` is null, and only while sealed.
    """

    version: str
    pulse_token: str
    # The envelope versions this agent speaks and the v2 digest of what it emits
    # (CORE-011 d2, d3); ``register.ok`` answers with the version the session uses.
    speaks: list[int]
    contract_digest: str
    # The v1 digest over ``integrations`` alone, kept through the overlap (d1).
    wire_contract: str
    commands: dict[str, Any]
    # CORE-005: per-Integration reports keyed by id.
    integrations: dict[str, IntegrationReport]
    log_groups: list[str]
    system_inventory: dict[str, Any]
    # The verify-block hatch's seal (ADR CORE-004) and the ISO-8601 UTC instant it
    # last opened: the agent owns that wall-clock, the dashboard never guesses it.
    signoff_sealed: bool
    unsealed_since: str | None
    # The kernel's boot id (``""`` where unreadable) and the process start, so a
    # reconnect is told from a reboot and from a restart.
    boot_id: str
    agent_started_at: str
    components: list[Component]
    # Request ids of long-running jobs still running at register; ids only.
    in_flight: list[str]

    @classmethod
    def from_dict(cls, data: Any) -> Self:
        return _payload_from_dict(
            cls,
            data,
            nested={"components": Component, "integrations": IntegrationReport},
        )


@dataclass(frozen=True, slots=True)
class SignoffStatePayload(WirePayload):
    """Payload for signoff.state messages (agent -> dashboard).

    Mid-session push of the verify-block seal state; register still carries
    the at-connect snapshot, this one stops the dashboard lagging a
    CLI-driven transition (ADR CORE-004 "Live propagation").
    """

    signoff_sealed: bool
    unsealed_since: str | None = None


@dataclass(frozen=True, slots=True)
class LogBatchPayload(WirePayload):
    """Payload for log.batch messages (agent -> dashboard)."""

    group: str
    parser: str
    batch_id: str
    lines: list[dict[str, Any]]
    dropped: int
    from_position: int | str
    to_position: int | str


@dataclass(frozen=True, slots=True)
class HeartbeatPayload(WirePayload):
    """Payload for heartbeat messages: empty by contract."""


@dataclass(frozen=True, slots=True)
class EventsBatchPayload(WirePayload):
    """Payload for events.batch messages (agent -> dashboard).

    Events are opaque dicts built by ``stormpulse.events.emit``; the control
    plane validates and maps them per field. ``batch_id`` comes back in the ack.
    """

    batch_id: str
    events: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class LogBatchAckPayload(WirePayload):
    """Payload for log.batch.ack (dashboard -> agent); the sender adds ``group``."""

    batch_id: str
    group: str | None = None


@dataclass(frozen=True, slots=True)
class EventsBatchAckPayload(WirePayload):
    """Payload for events.batch.ack (dashboard -> agent): the batch id alone."""

    batch_id: str


@dataclass(frozen=True, slots=True)
class EventsBatchNackPayload(WirePayload):
    """Payload for events.batch.nack (dashboard -> agent): the batch the control
    plane refused and why. The agent drops it, never re-sends it (CORE-011 d6)."""

    batch_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class RegisterOkPayload(WirePayload):
    """Payload for ``register.ok``: the version this session uses, from ``speaks``,
    and the control plane's own contract digest (CORE-011 d2, DEVELOPER-022 d2)."""

    protocol: int
    contract_digest: str


@dataclass(frozen=True, slots=True)
class MetricsAckPayload(WirePayload):
    """The specific metrics.push accepted by the control plane."""

    push_id: str


@dataclass(frozen=True, slots=True)
class AckPayload(WirePayload):
    """Payload for empty acknowledgements that have no delivery bookkeeping."""


@dataclass(frozen=True, slots=True)
class ErrorPayload(WirePayload):
    """Payload for ``error``, both ways: a reason code and the refused frame's ``type``.

    Never the refused payload, never the raw frame (CORE-011 d2).
    """

    error: str
    type: str | None = None


class Direction(StrEnum):
    """Which way a message type travels; the dispatcher routes inbound and both-way ones."""

    AGENT_TO_DASHBOARD = "agent->dashboard"
    DASHBOARD_TO_AGENT = "dashboard->agent"
    BOTH = "both"


@dataclass(frozen=True, slots=True)
class MessageSpec:
    """One row of the catalogue: where a type goes and the class its payload has."""

    direction: Direction
    payload: type[WirePayload]


# The catalogue: every MessageType, its direction and its payload class, so a
# generator, a dispatcher or a test iterates this instead of guessing (CORE-011 d3).
_OUT = Direction.AGENT_TO_DASHBOARD
_IN = Direction.DASHBOARD_TO_AGENT
MESSAGES: Mapping[MessageType, MessageSpec] = {
    MessageType.HEARTBEAT: MessageSpec(_OUT, HeartbeatPayload),
    MessageType.METRICS_PUSH: MessageSpec(_OUT, MetricsPayload),
    MessageType.COMMAND_RESULT: MessageSpec(_OUT, CommandResultPayload),
    MessageType.COMMAND_PROGRESS: MessageSpec(_OUT, CommandProgressPayload),
    MessageType.COMMAND_CANCELLED: MessageSpec(_OUT, CommandCancelledPayload),
    MessageType.REGISTER: MessageSpec(_OUT, RegisterPayload),
    MessageType.LOG_BATCH: MessageSpec(_OUT, LogBatchPayload),
    MessageType.EVENTS_BATCH: MessageSpec(_OUT, EventsBatchPayload),
    MessageType.SIGNOFF_STATE: MessageSpec(_OUT, SignoffStatePayload),
    MessageType.COMMAND_REQUEST: MessageSpec(_IN, CommandRequestPayload),
    MessageType.COMMAND_SEQUENCE: MessageSpec(_IN, CommandSequencePayload),
    MessageType.LOG_BATCH_ACK: MessageSpec(_IN, LogBatchAckPayload),
    MessageType.EVENTS_BATCH_ACK: MessageSpec(_IN, EventsBatchAckPayload),
    MessageType.EVENTS_BATCH_NACK: MessageSpec(_IN, EventsBatchNackPayload),
    MessageType.REGISTER_OK: MessageSpec(_IN, RegisterOkPayload),
    MessageType.HEARTBEAT_ACK: MessageSpec(_IN, AckPayload),
    MessageType.METRICS_ACK: MessageSpec(_IN, MetricsAckPayload),
    MessageType.COMMAND_RESULT_ACK: MessageSpec(_IN, AckPayload),
    MessageType.SIGNOFF_STATE_ACK: MessageSpec(_IN, AckPayload),
    MessageType.ERROR: MessageSpec(Direction.BOTH, ErrorPayload),
}


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
#: An ``error`` reply quotes at most this much of an unknown ``type``: never the frame.
_TYPE_QUOTE_MAX = 64


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
    # The frame as parsed, when one arrived: the MAC is checked over what the
    # sender signed, never a re-rendering of ``ts`` (CORE-011 d4). Off the wire.
    received: dict[str, Any] | None = field(
        default=None, compare=False, repr=False, metadata={"wire": False}
    )

    def __post_init__(self) -> None:
        check_version(self.v)
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

        v = check_version(data["v"])

        raw_type = data["type"]
        try:
            msg_type = MessageType(raw_type)
        except ValueError:
            raise ProtocolError(
                f"Unknown message type: {raw_type!r}",
                reason="unknown_type",
                msg_type=raw_type[:_TYPE_QUOTE_MAX]
                if isinstance(raw_type, str)
                else None,
            )

        ts = _parse_timestamp(data["ts"])

        agent_id = data["agent_id"]
        if not isinstance(agent_id, str) or not agent_id:
            raise ProtocolError("agent_id must be a non-empty string")

        payload = data["payload"]
        if not isinstance(payload, dict):
            raise ProtocolError(f"payload must be a dict, got {type(payload).__name__}")

        return cls(
            v=v,
            type=msg_type,
            id=data["id"],
            ts=ts,
            agent_id=agent_id,
            payload=payload,
            received=data,
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
        v=SPEAKS[-1],
        type=msg_type,
        id=str(uuid.uuid4()),
        ts=datetime.now(UTC),
        agent_id=agent_id,
        payload=payload,
    )


def make_error(agent_id: str, reason: str, msg_type: str | None) -> Envelope:
    """The ``error`` reply to a refused frame: its reason code and type, nothing else."""
    return _make_envelope(
        agent_id, MessageType.ERROR, asdict(ErrorPayload(error=reason, type=msg_type))
    )


def make_heartbeat(agent_id: str) -> Envelope:
    """Create a heartbeat envelope."""
    return _make_envelope(agent_id, MessageType.HEARTBEAT, {})


def make_register(
    agent_id: str,
    version: str,
    pulse_token: str,
    *,
    contract_digest: str,
    wire_contract: str,
    commands: dict[str, Any],
    integrations: dict[str, Any],
    log_groups: list[str],
    system_inventory: dict[str, Any],
    signoff_sealed: bool,
    unsealed_since: str | None,
    boot_id: str,
    agent_started_at: str,
    components: list[Component],
    in_flight: list[str],
) -> Envelope:
    """Create a register envelope; ``speaks`` is this module's ``SPEAKS``, never a caller's."""
    return _make_envelope(
        agent_id,
        MessageType.REGISTER,
        asdict(
            RegisterPayload(
                version=version,
                pulse_token=pulse_token,
                speaks=list(SPEAKS),
                contract_digest=contract_digest,
                wire_contract=wire_contract,
                commands=commands,
                integrations=integrations,
                log_groups=log_groups,
                system_inventory=system_inventory,
                signoff_sealed=signoff_sealed,
                unsealed_since=unsealed_since,
                boot_id=boot_id,
                agent_started_at=agent_started_at,
                components=components,
                in_flight=in_flight,
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

    ``integrations`` and ``job_load`` fill the payload's own ``integrations`` and
    ``jobs`` fields, so the bytes are ``asdict`` of the class the contract
    declares. ``jobs`` stays absent, not null, when there is no job manager.
    """
    if integrations is not None:
        metrics = replace(metrics, integrations=integrations)
    if job_load is not None:
        metrics = replace(metrics, jobs=job_load)
    payload = asdict(metrics)
    if payload["jobs"] is None:
        del payload["jobs"]
    return _make_envelope(agent_id, MessageType.METRICS_PUSH, payload)


def make_command_result(agent_id: str, payload: CommandResultPayload) -> Envelope:
    """Create a command.result envelope; the payload already carries its
    ``result`` and ``error`` (derived at construction on a failure)."""
    return _make_envelope(agent_id, MessageType.COMMAND_RESULT, asdict(payload))


def make_command_progress(agent_id: str, progress: CommandProgressPayload) -> Envelope:
    """Create a command.progress envelope."""
    return _make_envelope(agent_id, MessageType.COMMAND_PROGRESS, asdict(progress))


def make_command_cancelled(
    agent_id: str, *, request_id: str, command: str, reason: CancelReason
) -> Envelope:
    """Create a command.cancelled envelope: no command.result follows for this id."""
    return _make_envelope(
        agent_id,
        MessageType.COMMAND_CANCELLED,
        asdict(
            CommandCancelledPayload(
                request_id=request_id, command=command, reason=reason
            )
        ),
    )


def make_events_batch(
    agent_id: str,
    *,
    batch_id: str,
    events: list[dict[str, Any]],
) -> Envelope:
    """Create an events.batch envelope carrying wide events (``EventsBatchPayload``)."""
    return _make_envelope(
        agent_id,
        MessageType.EVENTS_BATCH,
        asdict(EventsBatchPayload(batch_id=batch_id, events=events)),
    )


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
