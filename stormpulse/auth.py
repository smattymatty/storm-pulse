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
from typing import Any

from stormpulse.protocol import (
    CommandRequestPayload,
    CommandSequencePayload,
    Envelope,
    MessageType,
)
from stormpulse.sdk.declaration import canonical_json

logger = logging.getLogger(__name__)


class AuthError(Exception):
    """Raised when a command fails HMAC, timestamp, or nonce verification.

    ``reason`` is a closed enum naming which check refused. A refused
    command is either a bug or an attack, and telling those apart needs
    to know which gate closed; parsing the human message to find out
    would make the distinction rot the first time the wording changes.
    """

    #: Every value ``reason`` may take. A refusal outside this set is a bug.
    REASONS = frozenset(  # skylos: ignore - documented closed enum; the contract, not a consumer
        {
            "secret_missing",
            "secret_empty",
            "wrong_type",
            "future_dated",
            "too_old",
            "bad_signature",
            "replayed_nonce",
            "unspecified",
        }
    )

    def __init__(self, message: str, *, reason: str = "unspecified") -> None:
        super().__init__(message)
        self.reason = reason


# Accept commands dated up to this many seconds in the future to tolerate
# minor clock skew between dashboard and agent. Anything further ahead is
# rejected so a future ts can't extend the replay window past nonce eviction.
_CLOCK_SKEW_TOLERANCE_SECONDS = 5


# Length of the raw key enrolment writes: HKDF-SHA256 output on the dashboard.
RAW_KEY_LEN = 32


def load_hmac_secret(path: Path) -> bytes:
    """Read the shared HMAC secret from a file.

    Two forms exist. Enrolment writes the raw 32-byte key HKDF derived
    on the dashboard (``RAW_KEY_LEN``); that form is returned untouched.
    Anything else is treated as a hand-written text secret and has its
    surrounding whitespace stripped. The distinction matters: a raw key
    whose first or last byte happens to be one of the six ASCII
    whitespace values (about one agent id in twenty) used to lose that
    byte to ``strip()``, and every dispatch to that agent then failed
    HMAC verification while registration still succeeded.
    Raises AuthError if missing or empty.
    """
    if not path.is_file():
        raise AuthError(f"HMAC secret file not found: {path}", reason="secret_missing")
    raw = path.read_bytes()
    if len(raw) != RAW_KEY_LEN:
        raw = raw.strip()
    if not raw:
        raise AuthError(f"HMAC secret file is empty: {path}", reason="secret_empty")
    return raw


def sign_envelope(envelope: dict[str, Any], secret: bytes) -> str:
    """HMAC-SHA256 hex over the envelope as ``canonical_json``, ``payload.hmac`` left out.

    Every key is covered: ``v``, ``type``, ``id``, ``ts``, ``agent_id`` and the
    whole payload, ``nonce`` included, so an additive field is signed for free
    and no two inputs share a form (CORE-011 d4). The dict is never mutated.
    Raises ValueError on non-JSON values or floats in signed data.
    """
    payload = dict(envelope["payload"])
    payload.pop("hmac", None)
    signed = {**envelope, "payload": payload}
    _check_signed_values(signed)
    message = canonical_json(signed)
    return hmac_mod.new(secret, message.encode("utf-8"), hashlib.sha256).hexdigest()


def _check_signed_values(value: Any) -> None:
    """Signed JSON uses integers, never floats; unsigned metrics may use floats."""
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("Signed JSON object keys must be strings")
            _check_signed_values(item)
    elif isinstance(value, list):
        for item in value:
            _check_signed_values(item)
    elif value is not None and type(value) not in (str, int, bool):
        raise ValueError(f"Signed JSON cannot contain {type(value).__name__}")


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

        Also purges nonces past max_age_seconds plus the skew tolerance.
        """
        # A command dated up to the skew tolerance ahead stays fresh that much
        # longer, so its nonce must outlive it by the same margin.
        cutoff = time.time() - max_age_seconds - _CLOCK_SKEW_TOLERANCE_SECONDS
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
                    raise AuthError(
                        f"Nonce already seen: {nonce!r}", reason="replayed_nonce"
                    )
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
    """Authenticate a command and return its checked payload.

    Check type, timestamp, HMAC, declared fields, then nonce uniqueness.
    Forged input never enters the nonce store. AuthError reports authentication
    failures; ProtocolError reports malformed authenticated payloads.
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

    # Authenticate the received frame before interpreting command fields.
    if envelope.received is None:
        raise AuthError(
            "Envelope was built in-process, not received off the wire",
            reason="bad_signature",
        )
    expected_hmac = envelope.payload.get("hmac")
    if (
        not isinstance(expected_hmac, str)
        or len(expected_hmac) != 64
        or any(c not in "0123456789abcdef" for c in expected_hmac)
    ):
        raise AuthError("HMAC verification failed", reason="bad_signature")
    try:
        computed = sign_envelope(envelope.received, secret)
    except ValueError as exc:
        raise AuthError(f"Envelope cannot be signed: {exc}", reason="bad_signature")
    if not hmac_mod.compare_digest(computed, expected_hmac):
        raise AuthError("HMAC verification failed", reason="bad_signature")

    payload: CommandRequestPayload | CommandSequencePayload
    if envelope.type == MessageType.COMMAND_REQUEST:
        payload = CommandRequestPayload.from_dict(envelope.payload)
    else:
        payload = CommandSequencePayload.from_dict(envelope.payload)

    # 4. Store only a valid, authenticated nonce.
    nonce_store.check_and_store(payload.nonce, max_age_seconds)

    return payload
