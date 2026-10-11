# skylos: ignore[SKY-Q502] every wire shape and the one catalogue that joins them live in one module (CORE-011 d3); a split would put the table and its rows in different files
"""Payloads: every message's dataclass, its type and the catalogue that joins them.

``MESSAGES`` is the one table a generator, a dispatcher or a test iterates
(CORE-011 d3). Shapes here are the wire; the artifact is rendered from them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from stormpulse.protocol.codec import WirePayload
from stormpulse.protocol.reasons import CancelReason, FailureReason


class MessageType(StrEnum):
    """All valid message types in the Storm Pulse protocol v2."""

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
    def failed(
        cls,
        request_id: str,
        command: str,
        group: str,
        reason: FailureReason,
        message: str,
        *,
        sequence_id: str | None = None,
        step_index: int | None = None,
        duration_ms: int = 0,
    ) -> CommandResultPayload:
        """The result for a command that left no output: refused, not found or
        never started. ``message`` is its stderr; ``error`` derives above."""
        return cls(
            request_id=request_id,
            command=command,
            group=group,
            success=False,
            exit_code=-1,
            stdout="",
            stderr=message,
            duration_ms=duration_ms,
            sequence_id=sequence_id,
            failure_reason=reason,
            step_index=step_index,
        )


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
    and the control plane's own contract digest (CORE-011 d2). The agent compares
    it with its local digest; on a mismatch the session stays up but commands
    are paused with that guidance until the two match again."""

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
