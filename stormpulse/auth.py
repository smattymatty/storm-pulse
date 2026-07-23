"""HMAC verification, nonce tracking, timestamp freshness."""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import logging
import secrets
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

from stormpulse.protocol import (
    CommandRequestPayload,
    CommandSequencePayload,
    Envelope,
    MessageType,
    format_timestamp,
)

logger = logging.getLogger(__name__)


class AuthError(Exception):
    """Raised when a command fails HMAC, timestamp, or nonce verification.

    ``reason`` is a closed enum naming which check refused. A refused
    command is either a bug or an attack, and telling those apart needs
    to know which gate closed; parsing the human message to find out
    would make the distinction rot the first time the wording changes.
    """

    #: Every value ``reason`` may take. A refusal outside this set is a bug.
    REASONS = frozenset({
        "secret_missing",
        "secret_empty",
        "wrong_type",
        "future_dated",
        "too_old",
        "bad_signature",
        "replayed_nonce",
        "unspecified",
    })

    def __init__(self, message: str, *, reason: str = "unspecified") -> None:
        super().__init__(message)
        self.reason = reason


# Accept commands dated up to this many seconds in the future to tolerate
# minor clock skew between dashboard and agent. Anything further ahead is
# rejected so a future ts can't extend the replay window past nonce eviction.
_CLOCK_SKEW_TOLERANCE_SECONDS = 5


def load_hmac_secret(path: Path) -> bytes:
    """Read the shared HMAC secret from a file.

    The file should contain the raw key bytes. Leading/trailing
    whitespace is stripped. Raises AuthError if missing or empty.
    """
    if not path.is_file():
        raise AuthError(f"HMAC secret file not found: {path}", reason="secret_missing")
    raw = path.read_bytes().strip()
    if not raw:
        raise AuthError(f"HMAC secret file is empty: {path}", reason="secret_empty")
    return raw


def canonicalize_params(params: dict[str, str]) -> str:
    """Canonicalize params for HMAC: sorted key=value pairs joined by &."""
    if not params:
        return ""
    return "&".join(f"{k}={v}" for k, v in sorted(params.items()))


def canonical_command_request(
    command: str,
    nonce: str,
    timestamp: str,
    params: dict[str, str] | None = None,
) -> str:
    """Build the canonical message for a command.request HMAC.

    Format: ``v1\\n{command}\\n{params_canonical}\\n{nonce}\\n{timestamp}``
    """
    params_str = canonicalize_params(params or {})
    return f"v1\n{command}\n{params_str}\n{nonce}\n{timestamp}"


def canonical_command_sequence(
    sequence_id: str,
    commands: list[str],
    stop_on_failure: bool,
    nonce: str,
    timestamp: str,
) -> str:
    """Build the canonical message for a command.sequence HMAC.

    Format: ``v1\\n{sequence_id}\\n{commands_csv}\\n{stop_on_failure}\\n{nonce}\\n{timestamp}``
    """
    commands_csv = ",".join(commands)
    stop_str = "true" if stop_on_failure else "false"
    return f"v1\n{sequence_id}\n{commands_csv}\n{stop_str}\n{nonce}\n{timestamp}"


def sign(message: str, secret: bytes) -> str:
    """Compute HMAC-SHA256 hex digest over a canonical message."""
    return hmac_mod.new(secret, message.encode("utf-8"), hashlib.sha256).hexdigest()


def generate_nonce() -> str:
    """Generate a cryptographically secure nonce (URL-safe, 32 bytes of entropy)."""
    return secrets.token_urlsafe(32)


class NonceStore:
    """SQLite-backed nonce tracker with lazy expiry cleanup.

    Each method operates within a transaction. SQLite WAL mode allows
    concurrent readers with a single writer.
    """

    def __init__(self, db_path: Path) -> None:
        self._conn = sqlite3.connect(str(db_path), timeout=5.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_table()

    def _ensure_table(self) -> None:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS seen_nonces ("
            "  nonce   TEXT PRIMARY KEY,"
            "  seen_at REAL NOT NULL"
            ")"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_seen_nonces_seen_at "
            "ON seen_nonces (seen_at)"
        )
        self._conn.commit()

    def check_and_store(self, nonce: str, max_age_seconds: int) -> None:
        """Record a nonce. Raises AuthError if already seen.

        Also purges expired nonces (older than max_age_seconds).
        """
        cutoff = time.time() - max_age_seconds
        try:
            with self._conn:
                self._conn.execute(
                    "DELETE FROM seen_nonces WHERE seen_at < ?", (cutoff,)
                )
                row = self._conn.execute(
                    "SELECT 1 FROM seen_nonces WHERE nonce = ?", (nonce,)
                ).fetchone()
                if row is not None:
                    # Raising here rolls back both the DELETE and any INSERT.
                    # Pruning work is lost but harmless - next call re-prunes.
                    raise AuthError(f"Nonce already seen: {nonce!r}", reason="replayed_nonce")
                self._conn.execute(
                    "INSERT INTO seen_nonces (nonce, seen_at) VALUES (?, ?)",
                    (nonce, time.time()),
                )
        except sqlite3.IntegrityError:
            raise AuthError(f"Nonce already seen: {nonce!r}", reason="replayed_nonce")

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()


def verify_envelope(
    envelope: Envelope,
    secret: bytes,
    nonce_store: NonceStore,
    max_age_seconds: int,
) -> CommandRequestPayload | CommandSequencePayload:
    """Verify an inbound command envelope.

    Checks in order:
    1. Message type is command.request or command.sequence.
    2. Timestamp freshness (within max_age_seconds of now).
    3. HMAC signature validity (constant-time comparison).
    4. Nonce uniqueness (stored in SQLite).

    Returns the parsed, typed payload on success.
    Raises AuthError on any failure.

    Order is deliberate: timestamp is cheapest (rejects stale before
    crypto), HMAC is checked before nonce storage (forged messages
    don't pollute the nonce store).
    """
    # 1. Type check
    if envelope.type not in (MessageType.COMMAND_REQUEST, MessageType.COMMAND_SEQUENCE):
        raise AuthError(
            f"Cannot verify non-command message type: {envelope.type.value}",
            reason="wrong_type",
        )

    # 2. Timestamp freshness - directional: reject future-dated commands
    # beyond a small skew tolerance so a future ts can't extend the replay
    # window past nonce eviction.
    now = datetime.now(UTC)
    age = (now - envelope.ts).total_seconds()
    if age < -_CLOCK_SKEW_TOLERANCE_SECONDS:
        raise AuthError(
            f"Command timestamp in the future: {-age:.1f}s ahead",
            reason="future_dated",
        )
    if age > max_age_seconds:
        raise AuthError(
            f"Command too old: {age:.1f}s > {max_age_seconds}s limit",
            reason="too_old",
        )

    # 3. Parse payload, build canonical message, verify HMAC
    ts_str = format_timestamp(envelope.ts)

    payload: CommandRequestPayload | CommandSequencePayload
    if envelope.type == MessageType.COMMAND_REQUEST:
        req_payload = CommandRequestPayload.from_dict(envelope.payload)
        canonical = canonical_command_request(
            req_payload.command,
            req_payload.nonce,
            ts_str,
            req_payload.params,
        )
        expected_hmac = req_payload.hmac
        nonce = req_payload.nonce
        payload = req_payload
    else:
        seq_payload = CommandSequencePayload.from_dict(envelope.payload)
        canonical = canonical_command_sequence(
            seq_payload.sequence_id,
            seq_payload.commands,
            seq_payload.stop_on_failure,
            seq_payload.nonce,
            ts_str,
        )
        expected_hmac = seq_payload.hmac
        nonce = seq_payload.nonce
        payload = seq_payload

    computed = sign(canonical, secret)
    if not hmac_mod.compare_digest(computed, expected_hmac):
        raise AuthError("HMAC verification failed", reason="bad_signature")

    # 4. Nonce uniqueness (only after HMAC passes)
    nonce_store.check_and_store(nonce, max_age_seconds)

    return payload
