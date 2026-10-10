"""Envelope: the frame every message rides in, and ``make``, the outbound factory.

``Envelope`` validates the frame and keeps the payload a raw dict; ``make``
builds one outbound frame from a payload dataclass with a fresh id and clock,
resolving the type from the catalogue. Three named factories carry logic.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Self

from stormpulse.protocol.codec import SPEAKS, ProtocolError, WirePayload, check_version
from stormpulse.protocol.payloads import (
    MESSAGES,
    Component,
    Direction,
    ErrorPayload,
    MessageType,
    MetricsPayload,
    RegisterPayload,
)


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

        message_id = data["id"]
        if not isinstance(message_id, str) or not message_id:
            raise ProtocolError("id must be a non-empty string")

        agent_id = data["agent_id"]
        if not isinstance(agent_id, str) or not agent_id:
            raise ProtocolError("agent_id must be a non-empty string")

        payload = data["payload"]
        if not isinstance(payload, dict):
            raise ProtocolError(f"payload must be a dict, got {type(payload).__name__}")

        return cls(
            v=v,
            type=msg_type,
            id=message_id,
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


# The catalogue reversed for this side: outbound and both-way rows only, since
# ``AckPayload`` rides under three inbound types and the full map is not injective.
_OUTBOUND_ROWS = [
    (msg_type, spec.payload)
    for msg_type, spec in MESSAGES.items()
    if spec.direction is not Direction.DASHBOARD_TO_AGENT
]
_OUTBOUND_TYPE: dict[type[WirePayload], MessageType] = {
    payload: msg_type for msg_type, payload in _OUTBOUND_ROWS
}
if len(_OUTBOUND_TYPE) != len(_OUTBOUND_ROWS):
    raise RuntimeError("MESSAGES: an outbound payload class serves two message types")


def make(agent_id: str, payload: WirePayload) -> Envelope:
    """One outbound frame: the type is the catalogue's for the payload's class,
    the bytes are ``asdict`` of it. An inbound-only class is a caller's bug;
    ``metrics.push`` has its own door, since ``jobs`` is absent there, not null."""
    if isinstance(payload, MetricsPayload):
        raise TypeError("MetricsPayload rides through make_metrics_push")
    try:
        msg_type = _OUTBOUND_TYPE[type(payload)]
    except KeyError:
        raise TypeError(
            f"{type(payload).__name__} is not an outbound payload"
        ) from None
    return _make_envelope(agent_id, msg_type, asdict(payload))


def make_error(agent_id: str, reason: str, msg_type: str | None) -> Envelope:
    """The ``error`` reply to a refused frame: its reason code and type, nothing else."""
    return make(agent_id, ErrorPayload(error=reason, type=msg_type))


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
    return make(
        agent_id,
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
        ),
    )


def make_metrics_push(agent_id: str, metrics: MetricsPayload) -> Envelope:
    """The one door for ``metrics.push``: ``asdict`` of the payload, with ``jobs``
    absent, not null, when there is no job manager."""
    payload = asdict(metrics)
    if payload["jobs"] is None:
        del payload["jobs"]
    return _make_envelope(agent_id, MessageType.METRICS_PUSH, payload)
