"""Tests for stormpulse.auth."""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stormpulse.auth import (
    AuthError,
    NonceStore,
    canonical_command_request,
    canonical_command_sequence,
    canonicalize_params,
    generate_nonce,
    load_hmac_secret,
    sign,
    verify_envelope,
)
from stormpulse.protocol import (
    CommandRequestPayload,
    CommandSequencePayload,
    Envelope,
    MessageType,
    format_timestamp,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SECRET = b"test-secret-key-256-bits-long!!!"


@pytest.fixture
def secret_path(tmp_path: Path) -> Path:
    p = tmp_path / "hmac.key"
    p.write_bytes(SECRET)
    return p


@pytest.fixture
def nonce_store(tmp_path: Path) -> Generator[NonceStore, None, None]:
    store = NonceStore(tmp_path / "test.db")
    yield store
    store.close()


# ---------------------------------------------------------------------------
# Helpers - build signed envelopes (mirrors dashboard signing)
# ---------------------------------------------------------------------------


def _make_signed_request(
    command: str = "git_pull",
    secret: bytes = SECRET,
    *,
    agent_id: str = "test-agent",
    nonce: str | None = None,
    ts: datetime | None = None,
    params: dict[str, str] | None = None,
) -> Envelope:
    if ts is None:
        ts = datetime.now(UTC)
    if nonce is None:
        nonce = generate_nonce()
    ts_str = format_timestamp(ts)
    canonical = canonical_command_request(command, nonce, ts_str, params)
    sig = sign(canonical, secret)
    return Envelope(
        v=1,
        type=MessageType.COMMAND_REQUEST,
        id=str(uuid.uuid4()),
        ts=ts,
        agent_id=agent_id,
        payload={
            "command": command,
            "params": params or {},
            "hmac": sig,
            "nonce": nonce,
        },
    )


def _make_signed_sequence(
    commands: list[str] | None = None,
    secret: bytes = SECRET,
    *,
    sequence_id: str = "seq-001",
    stop_on_failure: bool = True,
    agent_id: str = "test-agent",
    nonce: str | None = None,
    ts: datetime | None = None,
) -> Envelope:
    if commands is None:
        commands = ["git_pull", "docker_logs"]
    if ts is None:
        ts = datetime.now(UTC)
    if nonce is None:
        nonce = generate_nonce()
    ts_str = format_timestamp(ts)
    canonical = canonical_command_sequence(
        sequence_id, commands, stop_on_failure, nonce, ts_str
    )
    sig = sign(canonical, secret)
    return Envelope(
        v=1,
        type=MessageType.COMMAND_SEQUENCE,
        id=str(uuid.uuid4()),
        ts=ts,
        agent_id=agent_id,
        payload={
            "sequence_id": sequence_id,
            "commands": commands,
            "stop_on_failure": stop_on_failure,
            "hmac": sig,
            "nonce": nonce,
        },
    )


# ---------------------------------------------------------------------------
# HMAC secret loading
# ---------------------------------------------------------------------------


def test_load_hmac_secret_valid(secret_path: Path) -> None:
    key = load_hmac_secret(secret_path)
    assert key == SECRET


def test_load_hmac_secret_missing_file(tmp_path: Path) -> None:
    with pytest.raises(AuthError, match="not found"):
        load_hmac_secret(tmp_path / "nonexistent.key")


def test_load_hmac_secret_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "empty.key"
    p.write_bytes(b"")
    with pytest.raises(AuthError, match="empty"):
        load_hmac_secret(p)


def test_load_hmac_secret_strips_whitespace(tmp_path: Path) -> None:
    p = tmp_path / "hmac.key"
    p.write_bytes(b"my-secret-key\n")
    key = load_hmac_secret(p)
    assert key == b"my-secret-key"


# ---------------------------------------------------------------------------
# Canonical message construction
# ---------------------------------------------------------------------------


def test_canonical_command_request_format() -> None:
    result = canonical_command_request("git_pull", "nonce-1", "2026-02-21T12:00:00Z")
    assert result == "v1\ngit_pull\n\nnonce-1\n2026-02-21T12:00:00Z"


def test_canonical_command_request_with_params() -> None:
    result = canonical_command_request(
        "docker_logs",
        "nonce-1",
        "2026-02-21T12:00:00Z",
        params={"service": "celery", "count": "50"},
    )
    assert (
        result
        == "v1\ndocker_logs\ncount=50&service=celery\nnonce-1\n2026-02-21T12:00:00Z"
    )


def test_canonical_command_request_v1_prefix() -> None:
    result = canonical_command_request("x", "y", "z")
    assert result.startswith("v1\n")


def test_canonical_command_sequence_format() -> None:
    result = canonical_command_sequence(
        "seq-001", ["git_pull", "docker_logs"], True, "nonce-2", "2026-02-21T12:00:00Z"
    )
    assert (
        result
        == "v1\nseq-001\ngit_pull,docker_logs\ntrue\nnonce-2\n2026-02-21T12:00:00Z"
    )


def test_canonical_command_sequence_stop_on_failure_false() -> None:
    result = canonical_command_sequence("s", ["a"], False, "n", "t")
    assert "\nfalse\n" in result


def test_canonical_command_sequence_single_command() -> None:
    result = canonical_command_sequence("s", ["git_pull"], True, "n", "t")
    assert "\ngit_pull\n" in result


# ---------------------------------------------------------------------------
# Signing primitives
# ---------------------------------------------------------------------------


def test_sign_returns_hex_string() -> None:
    sig = sign("test message", SECRET)
    assert isinstance(sig, str)
    assert len(sig) == 64  # SHA-256 hex = 64 chars
    int(sig, 16)  # must be valid hex


def test_sign_deterministic() -> None:
    sig1 = sign("same message", SECRET)
    sig2 = sign("same message", SECRET)
    assert sig1 == sig2


def test_sign_different_messages() -> None:
    sig1 = sign("message-a", SECRET)
    sig2 = sign("message-b", SECRET)
    assert sig1 != sig2


def test_sign_different_secrets() -> None:
    sig1 = sign("same message", b"secret-1")
    sig2 = sign("same message", b"secret-2")
    assert sig1 != sig2


def test_generate_nonce_unique() -> None:
    n1 = generate_nonce()
    n2 = generate_nonce()
    assert n1 != n2


def test_generate_nonce_length() -> None:
    n = generate_nonce()
    assert len(n) >= 40  # base64url of 32 bytes ~ 43 chars


# ---------------------------------------------------------------------------
# Nonce store
# ---------------------------------------------------------------------------


def test_nonce_store_creates_table(tmp_path: Path) -> None:
    store = NonceStore(tmp_path / "nonces.db")
    conn = sqlite3.connect(str(tmp_path / "nonces.db"))
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    conn.close()
    store.close()
    assert ("seen_nonces",) in tables


def test_nonce_store_accepts_new_nonce(nonce_store: NonceStore) -> None:
    nonce_store.check_and_store("fresh-nonce", max_age_seconds=60)


def test_nonce_store_rejects_duplicate(nonce_store: NonceStore) -> None:
    nonce_store.check_and_store("dup-nonce", max_age_seconds=60)
    with pytest.raises(AuthError, match="already seen"):
        nonce_store.check_and_store("dup-nonce", max_age_seconds=60)


def test_nonce_store_purges_expired(nonce_store: NonceStore) -> None:
    nonce_store.check_and_store("old-nonce", max_age_seconds=60)
    # Backdate the nonce so it appears expired
    nonce_store._conn.execute(
        "UPDATE seen_nonces SET seen_at = ? WHERE nonce = ?",
        (time.time() - 120, "old-nonce"),
    )
    nonce_store._conn.commit()
    # Now it should be purged and accepted again
    nonce_store.check_and_store("old-nonce", max_age_seconds=60)


def test_nonce_store_close_and_reopen(tmp_path: Path) -> None:
    db = tmp_path / "reopen.db"
    store1 = NonceStore(db)
    store1.check_and_store("persist-nonce", max_age_seconds=60)
    store1.close()
    store2 = NonceStore(db)
    with pytest.raises(AuthError, match="already seen"):
        store2.check_and_store("persist-nonce", max_age_seconds=60)
    store2.close()


# ---------------------------------------------------------------------------
# verify_envelope - command.request
# ---------------------------------------------------------------------------


def test_verify_valid_command_request(nonce_store: NonceStore) -> None:
    env = _make_signed_request()
    payload = verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert isinstance(payload, CommandRequestPayload)
    assert payload.command == "git_pull"


def test_verify_command_request_returns_typed_payload(nonce_store: NonceStore) -> None:
    env = _make_signed_request(command="docker_logs")
    payload = verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert isinstance(payload, CommandRequestPayload)
    assert payload.command == "docker_logs"
    assert isinstance(payload.nonce, str)


def test_verify_command_request_bad_hmac(nonce_store: NonceStore) -> None:
    env = _make_signed_request()
    # Tamper with the HMAC
    env.payload["hmac"] = "0" * 64
    with pytest.raises(AuthError, match="HMAC"):
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)


def test_verify_command_request_stale(nonce_store: NonceStore) -> None:
    old_ts = datetime.now(UTC) - timedelta(seconds=120)
    env = _make_signed_request(ts=old_ts)
    with pytest.raises(AuthError, match="old"):
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)


def test_verify_command_request_replayed_nonce(nonce_store: NonceStore) -> None:
    nonce = generate_nonce()
    env1 = _make_signed_request(nonce=nonce)
    verify_envelope(env1, SECRET, nonce_store, max_age_seconds=60)
    env2 = _make_signed_request(nonce=nonce)
    with pytest.raises(AuthError, match="already seen"):
        verify_envelope(env2, SECRET, nonce_store, max_age_seconds=60)


# ---------------------------------------------------------------------------
# verify_envelope - command.sequence
# ---------------------------------------------------------------------------


def test_verify_valid_command_sequence(nonce_store: NonceStore) -> None:
    env = _make_signed_sequence()
    payload = verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert isinstance(payload, CommandSequencePayload)
    assert payload.sequence_id == "seq-001"


def test_verify_command_sequence_returns_typed_payload(nonce_store: NonceStore) -> None:
    env = _make_signed_sequence(commands=["git_pull", "docker_logs"])
    payload = verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert isinstance(payload, CommandSequencePayload)
    assert payload.commands == ["git_pull", "docker_logs"]
    assert payload.stop_on_failure is True


def test_verify_command_sequence_bad_hmac(nonce_store: NonceStore) -> None:
    env = _make_signed_sequence()
    env.payload["hmac"] = "bad" * 21 + "b"
    with pytest.raises(AuthError, match="HMAC"):
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)


def test_verify_command_sequence_stale(nonce_store: NonceStore) -> None:
    old_ts = datetime.now(UTC) - timedelta(seconds=120)
    env = _make_signed_sequence(ts=old_ts)
    with pytest.raises(AuthError, match="old"):
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)


def test_verify_command_sequence_replayed_nonce(nonce_store: NonceStore) -> None:
    nonce = generate_nonce()
    env1 = _make_signed_sequence(nonce=nonce)
    verify_envelope(env1, SECRET, nonce_store, max_age_seconds=60)
    env2 = _make_signed_sequence(nonce=nonce)
    with pytest.raises(AuthError, match="already seen"):
        verify_envelope(env2, SECRET, nonce_store, max_age_seconds=60)


# ---------------------------------------------------------------------------
# verify_envelope - wrong message types
# ---------------------------------------------------------------------------


def test_verify_heartbeat_raises(nonce_store: NonceStore) -> None:
    env = Envelope(
        v=1,
        type=MessageType.HEARTBEAT,
        id=str(uuid.uuid4()),
        ts=datetime.now(UTC),
        agent_id="test-agent",
        payload={},
    )
    with pytest.raises(AuthError, match="non-command"):
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)


def test_verify_metrics_push_raises(nonce_store: NonceStore) -> None:
    env = Envelope(
        v=1,
        type=MessageType.METRICS_PUSH,
        id=str(uuid.uuid4()),
        ts=datetime.now(UTC),
        agent_id="test-agent",
        payload={"cpu_percent": 0},
    )
    with pytest.raises(AuthError, match="non-command"):
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)


def test_verify_register_raises(nonce_store: NonceStore) -> None:
    env = Envelope(
        v=1,
        type=MessageType.REGISTER,
        id=str(uuid.uuid4()),
        ts=datetime.now(UTC),
        agent_id="test-agent",
        payload={"version": "0.1.0"},
    )
    with pytest.raises(AuthError, match="non-command"):
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)


# ---------------------------------------------------------------------------
# Verification order (security-critical)
# ---------------------------------------------------------------------------


def test_stale_rejected_before_hmac_check(nonce_store: NonceStore) -> None:
    """A stale message with a bad HMAC should fail on timestamp, not HMAC."""
    old_ts = datetime.now(UTC) - timedelta(seconds=120)
    env = Envelope(
        v=1,
        type=MessageType.COMMAND_REQUEST,
        id=str(uuid.uuid4()),
        ts=old_ts,
        agent_id="test-agent",
        payload={"command": "git_pull", "params": {}, "hmac": "bad", "nonce": "n"},
    )
    with pytest.raises(AuthError, match="old"):
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)


def test_bad_hmac_rejected_before_nonce_stored(nonce_store: NonceStore) -> None:
    """A forged message should not store its nonce."""
    nonce = "should-not-be-stored"
    env = Envelope(
        v=1,
        type=MessageType.COMMAND_REQUEST,
        id=str(uuid.uuid4()),
        ts=datetime.now(UTC),
        agent_id="test-agent",
        payload={"command": "git_pull", "params": {}, "hmac": "bad", "nonce": nonce},
    )
    with pytest.raises(AuthError, match="HMAC"):
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    # Nonce should NOT have been stored
    row = nonce_store._conn.execute(
        "SELECT 1 FROM seen_nonces WHERE nonce = ?", (nonce,)
    ).fetchone()
    assert row is None


# ---------------------------------------------------------------------------
# Round-trip: sign then verify
# ---------------------------------------------------------------------------


def test_sign_verify_command_request_roundtrip(nonce_store: NonceStore) -> None:
    env = _make_signed_request(command="docker_logs")
    payload = verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert payload.command == "docker_logs"  # type: ignore[union-attr]


def test_sign_verify_command_sequence_roundtrip(nonce_store: NonceStore) -> None:
    cmds = ["git_pull", "docker_logs"]
    env = _make_signed_sequence(commands=cmds, stop_on_failure=False)
    payload = verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert isinstance(payload, CommandSequencePayload)
    assert payload.commands == cmds
    assert payload.stop_on_failure is False


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_verify_non_utc_timezone(nonce_store: NonceStore) -> None:
    """A timestamp with a non-UTC offset should still verify if fresh."""
    from datetime import timezone as tz

    offset = tz(timedelta(hours=5, minutes=30))
    ts = datetime.now(offset)
    env = _make_signed_request(ts=ts)
    payload = verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert isinstance(payload, CommandRequestPayload)


def test_verify_future_timestamp_within_window(nonce_store: NonceStore) -> None:
    """A slightly future timestamp (clock skew) within window should pass."""
    future_ts = datetime.now(UTC) + timedelta(seconds=5)
    env = _make_signed_request(ts=future_ts)
    payload = verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert isinstance(payload, CommandRequestPayload)


def test_verify_future_timestamp_too_far_raises(nonce_store: NonceStore) -> None:
    """A future timestamp beyond the skew tolerance should be rejected."""
    far_future = datetime.now(UTC) + timedelta(seconds=120)
    env = _make_signed_request(ts=far_future)
    with pytest.raises(AuthError, match="future"):
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)


def test_empty_command_list_in_sequence(nonce_store: NonceStore) -> None:
    """An empty command list should still produce a valid canonical message."""
    env = _make_signed_sequence(commands=[])
    payload = verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert isinstance(payload, CommandSequencePayload)
    assert payload.commands == []


# ---------------------------------------------------------------------------
# canonicalize_params
# ---------------------------------------------------------------------------


def test_canonicalize_params_empty() -> None:
    assert canonicalize_params({}) == ""


def test_canonicalize_params_sorted() -> None:
    result = canonicalize_params({"z": "3", "a": "1", "m": "2"})
    assert result == "a=1&m=2&z=3"


def test_canonicalize_params_single() -> None:
    assert canonicalize_params({"service": "web"}) == "service=web"


# ---------------------------------------------------------------------------
# verify_envelope with params
# ---------------------------------------------------------------------------


def test_verify_command_request_with_params_roundtrip(nonce_store: NonceStore) -> None:
    env = _make_signed_request(
        command="docker_logs",
        params={"service": "celery"},
    )
    payload = verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert isinstance(payload, CommandRequestPayload)
    assert payload.command == "docker_logs"
    assert payload.params == {"service": "celery"}
