"""Tests for stormpulse.auth."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from collections.abc import Generator
from dataclasses import asdict, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from stormpulse.auth import (
    AuthError,
    NonceStore,
    generate_nonce,
    load_hmac_secret,
    sign_envelope,
    verify_envelope,
)
from stormpulse.protocol import (
    MESSAGES,
    CommandRequestPayload,
    CommandSequencePayload,
    Direction,
    Envelope,
    MessageType,
    dataclass_wire_types,
)
from tests.helpers import (
    SECRET,
    envelope,
    signed_command_request,
    signed_command_sequence,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


def test_load_hmac_secret_keeps_raw_key_whitespace_bytes(tmp_path: Path) -> None:
    """A raw 32-byte key is returned byte for byte, even when its first or
    last byte is an ASCII whitespace value. Enrolment writes raw HKDF
    output, so about one agent id in twenty produces such a key; stripping
    it made every dispatch to that agent fail HMAC while registration
    (mTLS + pulse token) still succeeded. Found on forgejo_w_1, 2026-09-20."""
    raw = b"\n" + bytes(range(1, 31)) + b" "
    assert len(raw) == 32
    p = tmp_path / "hmac.key"
    p.write_bytes(raw)
    assert load_hmac_secret(p) == raw


# ---------------------------------------------------------------------------
# sign_envelope: the whole envelope as canonical JSON (CORE-011 d4)  # skylos: ignore[SKY-A101] line-form negatives deleted per d4; refusals live in verify_envelope tests
# ---------------------------------------------------------------------------


def _request_dict(**overrides: Any) -> dict[str, Any]:
    """A fixed, unsigned command.request envelope dict; ``overrides`` replace top-level keys."""
    base = envelope(
        MessageType.COMMAND_REQUEST,
        {"command": "git_pull", "params": {}, "nonce": "nonce-1"},
        id="550e8400-e29b-41d4-a716-446655440002",
        ts=datetime(2026, 2, 21, 12, 0, tzinfo=UTC),
    ).to_dict()
    return {**base, **overrides}


def test_sign_envelope_refuses_a_nan() -> None:
    """No signer may cover a NaN (Function 15): refused, never a signature."""
    env = _request_dict(
        payload={"command": "c", "params": {"x": float("nan")}, "nonce": "n"}
    )
    with pytest.raises(ValueError):
        sign_envelope(env, SECRET)


def test_sign_envelope_returns_hex_sha256() -> None:
    sig = sign_envelope(_request_dict(), SECRET)
    assert isinstance(sig, str)
    assert len(sig) == 64  # SHA-256 hex = 64 chars
    int(sig, 16)  # must be valid hex


def test_sign_envelope_deterministic() -> None:
    assert sign_envelope(_request_dict(), SECRET) == sign_envelope(
        _request_dict(), SECRET
    )


def test_sign_envelope_different_secrets() -> None:
    assert sign_envelope(_request_dict(), b"secret-1") != sign_envelope(
        _request_dict(), b"secret-2"
    )


@pytest.mark.parametrize("key", ["v", "type", "id", "ts", "agent_id"])
def test_sign_envelope_covers_every_envelope_key(key: str) -> None:
    """The v1 line form covered neither ``id`` nor ``agent_id``; v2 covers them all."""
    base = _request_dict()
    changed = {**base, key: 3 if key == "v" else f"other-{key}"}
    assert sign_envelope(base, SECRET) != sign_envelope(changed, SECRET)


def test_sign_envelope_covers_every_payload_field() -> None:
    base = _request_dict()
    for field_name in ("command", "params", "nonce"):
        changed = {**base, "payload": {**base["payload"], field_name: "changed"}}
        assert sign_envelope(base, SECRET) != sign_envelope(changed, SECRET)


def test_sign_envelope_leaves_the_hmac_out() -> None:
    """Whatever ``payload.hmac`` holds, the signed bytes are the same."""
    base = _request_dict()
    with_hmac = {**base, "payload": {**base["payload"], "hmac": "0" * 64}}
    assert sign_envelope(base, SECRET) == sign_envelope(with_hmac, SECRET)


def test_sign_envelope_never_mutates_its_input() -> None:
    env = _request_dict(
        payload={"command": "x", "params": {}, "nonce": "n", "hmac": "h"}
    )
    before = json.dumps(env, sort_keys=True)
    sign_envelope(env, SECRET)
    assert json.dumps(env, sort_keys=True) == before
    assert env["payload"]["hmac"] == "h"


def test_sign_envelope_is_injective_where_the_line_form_was_not() -> None:
    """``a=1&b=2`` and ``{a: 1&b=2}`` shared one v1 line; JSON keeps them apart."""
    joined = _request_dict(
        payload={"command": "c", "params": {"a": "1&b=2"}, "nonce": "n"}
    )
    split = _request_dict(
        payload={"command": "c", "params": {"a": "1", "b": "2"}, "nonce": "n"}
    )
    assert sign_envelope(joined, SECRET) != sign_envelope(split, SECRET)


def test_sign_envelope_ignores_key_order() -> None:
    base = _request_dict()
    reordered = dict(reversed(list(base.items())))
    assert list(reordered) != list(base)
    assert sign_envelope(base, SECRET) == sign_envelope(reordered, SECRET)


def test_no_float_rides_in_any_payload_sent_to_the_agent() -> None:
    """No floats in anything signed (CORE-011 d4): integers and strings only
    in every dashboard->agent payload class. Metrics are outbound and unsigned."""
    numbers = [
        f"{cls}.{name}"
        for spec in MESSAGES.values()
        if spec.direction is Direction.DASHBOARD_TO_AGENT
        for cls, declared in dataclass_wire_types(spec.payload).items()
        for name, typed in declared.items()
        if "number"
        in (typed["type"] if isinstance(typed["type"], list) else [typed["type"]])
    ]
    assert numbers == []


def test_received_frame_is_off_the_wire_walk() -> None:
    """``Envelope.received`` is bookkeeping, never a wire field: the walkers skip it."""
    assert "received" in {f.name for f in fields(Envelope)}
    assert (
        "received"
        not in dataclass_wire_types(Envelope, formatted={"ts": "string"})["Envelope"]
    )


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
    env = signed_command_request()
    payload = verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert isinstance(payload, CommandRequestPayload)
    assert payload.command == "git_pull"


def test_verify_command_request_returns_typed_payload(nonce_store: NonceStore) -> None:
    env = signed_command_request(command="docker_logs")
    payload = verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert isinstance(payload, CommandRequestPayload)
    assert payload.command == "docker_logs"
    assert isinstance(payload.nonce, str)


def test_verify_command_request_bad_hmac(nonce_store: NonceStore) -> None:
    env = signed_command_request()
    # Tamper with the HMAC
    env.payload["hmac"] = "0" * 64
    with pytest.raises(AuthError, match="HMAC"):
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)


def test_verify_command_request_stale(nonce_store: NonceStore) -> None:
    old_ts = datetime.now(UTC) - timedelta(seconds=120)
    env = signed_command_request(ts=old_ts)
    with pytest.raises(AuthError, match="old"):
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)


def test_verify_command_request_replayed_nonce(nonce_store: NonceStore) -> None:
    nonce = generate_nonce()
    env1 = signed_command_request(nonce=nonce)
    verify_envelope(env1, SECRET, nonce_store, max_age_seconds=60)
    env2 = signed_command_request(nonce=nonce)
    with pytest.raises(AuthError, match="already seen"):
        verify_envelope(env2, SECRET, nonce_store, max_age_seconds=60)


# ---------------------------------------------------------------------------
# verify_envelope - command.sequence
# ---------------------------------------------------------------------------


def test_verify_valid_command_sequence(nonce_store: NonceStore) -> None:
    env = signed_command_sequence(sequence_id="seq-001")
    payload = verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert isinstance(payload, CommandSequencePayload)
    assert payload.sequence_id == "seq-001"


def test_verify_command_sequence_returns_typed_payload(nonce_store: NonceStore) -> None:
    env = signed_command_sequence(commands=["git_pull", "docker_logs"])
    payload = verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert isinstance(payload, CommandSequencePayload)
    assert [s.command for s in payload.steps] == ["git_pull", "docker_logs"]
    assert payload.stop_on_failure is True


def test_verify_command_sequence_bad_hmac(nonce_store: NonceStore) -> None:
    env = signed_command_sequence()
    env.payload["hmac"] = "bad" * 21 + "b"
    with pytest.raises(AuthError, match="HMAC"):
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)


def test_verify_command_sequence_stale(nonce_store: NonceStore) -> None:
    old_ts = datetime.now(UTC) - timedelta(seconds=120)
    env = signed_command_sequence(ts=old_ts)
    with pytest.raises(AuthError, match="old"):
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)


def test_verify_command_sequence_replayed_nonce(nonce_store: NonceStore) -> None:
    nonce = generate_nonce()
    env1 = signed_command_sequence(nonce=nonce)
    verify_envelope(env1, SECRET, nonce_store, max_age_seconds=60)
    env2 = signed_command_sequence(nonce=nonce)
    with pytest.raises(AuthError, match="already seen"):
        verify_envelope(env2, SECRET, nonce_store, max_age_seconds=60)


# ---------------------------------------------------------------------------
# verify_envelope - wrong message types
# ---------------------------------------------------------------------------


def test_verify_heartbeat_raises(nonce_store: NonceStore) -> None:
    env = envelope(MessageType.HEARTBEAT, {}, agent_id="test-agent")
    with pytest.raises(AuthError, match="non-command"):
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)


def test_verify_metrics_push_raises(nonce_store: NonceStore) -> None:
    env = envelope(MessageType.METRICS_PUSH, {"cpu_percent": 0}, agent_id="test-agent")
    with pytest.raises(AuthError, match="non-command"):
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)


def test_verify_register_raises(nonce_store: NonceStore) -> None:
    env = envelope(MessageType.REGISTER, {"version": "0.1.0"}, agent_id="test-agent")
    with pytest.raises(AuthError, match="non-command"):
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)


# ---------------------------------------------------------------------------
# Verification order (security-critical)
# ---------------------------------------------------------------------------


def test_stale_rejected_before_hmac_check(nonce_store: NonceStore) -> None:
    """A stale message with a bad HMAC should fail on timestamp, not HMAC."""
    old_ts = datetime.now(UTC) - timedelta(seconds=120)
    env = envelope(
        MessageType.COMMAND_REQUEST,
        {"command": "git_pull", "params": {}, "hmac": "bad", "nonce": "n"},
        agent_id="test-agent",
        ts=old_ts,
    )
    with pytest.raises(AuthError, match="old"):
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)


def test_bad_hmac_rejected_before_nonce_stored(nonce_store: NonceStore) -> None:
    """A forged message should not store its nonce."""
    nonce = "should-not-be-stored"
    env = Envelope.from_json(
        envelope(
            MessageType.COMMAND_REQUEST,
            {"command": "git_pull", "params": {}, "hmac": "bad", "nonce": nonce},
            agent_id="test-agent",
        ).to_json()
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
    env = signed_command_request(command="docker_logs")
    payload = verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert payload.command == "docker_logs"  # type: ignore[union-attr]


def test_sign_verify_command_sequence_roundtrip(nonce_store: NonceStore) -> None:
    cmds = ["git_pull", "docker_logs"]
    env = signed_command_sequence(commands=cmds, stop_on_failure=False)
    payload = verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert isinstance(payload, CommandSequencePayload)
    assert [s.command for s in payload.steps] == cmds
    assert payload.stop_on_failure is False


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_verify_non_utc_timezone(nonce_store: NonceStore) -> None:
    """A timestamp with a non-UTC offset should still verify if fresh."""
    from datetime import timezone as tz

    offset = tz(timedelta(hours=5, minutes=30))
    ts = datetime.now(offset)
    env = signed_command_request(ts=ts)
    payload = verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert isinstance(payload, CommandRequestPayload)


def test_verify_future_timestamp_within_window(nonce_store: NonceStore) -> None:
    """A slightly future timestamp (clock skew) within window should pass."""
    future_ts = datetime.now(UTC) + timedelta(seconds=5)
    env = signed_command_request(ts=future_ts)
    payload = verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert isinstance(payload, CommandRequestPayload)


def test_verify_future_timestamp_too_far_raises(nonce_store: NonceStore) -> None:
    """A future timestamp beyond the skew tolerance should be rejected."""
    far_future = datetime.now(UTC) + timedelta(seconds=120)
    env = signed_command_request(ts=far_future)
    with pytest.raises(AuthError, match="future"):
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)


def test_future_dated_command_cannot_replay_after_nonce_eviction(
    nonce_store: NonceStore,
) -> None:
    """A future-dated command stays fresh past max_age, so its nonce must too."""
    env = signed_command_request(ts=datetime.now(UTC) - timedelta(seconds=58))
    verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    # First seen 62s ago: the command was dated 4s ahead and is still fresh.
    nonce_store._conn.execute("UPDATE seen_nonces SET seen_at = ?", (time.time() - 62,))
    nonce_store._conn.commit()
    with pytest.raises(AuthError) as exc_info:
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert exc_info.value.reason == "replayed_nonce"


def test_empty_command_list_in_sequence(nonce_store: NonceStore) -> None:
    """An empty command list still signs and verifies."""
    env = signed_command_sequence(commands=[])
    payload = verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert isinstance(payload, CommandSequencePayload)
    assert payload.steps == []


# ---------------------------------------------------------------------------
# verify_envelope with params
# ---------------------------------------------------------------------------


def test_verify_command_request_with_params_roundtrip(nonce_store: NonceStore) -> None:
    env = signed_command_request(
        command="docker_logs",
        params={"service": "celery"},
    )
    payload = verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert isinstance(payload, CommandRequestPayload)
    assert payload.command == "docker_logs"
    assert payload.params == {"service": "celery"}


def test_verify_command_request_with_a_nested_param(nonce_store: NonceStore) -> None:
    """A typed param (CORE-011 d9) is an object; the line form could not sign it."""
    rules = {"cors": [{"origins": ["https://a.example"], "methods": ["GET"]}], "n": 3}
    env = signed_command_request(command="caddy_cors", params={"rules": rules})
    payload = verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert isinstance(payload, CommandRequestPayload)
    assert asdict(payload)["params"] == {"rules": rules}


# ---------------------------------------------------------------------------
# verify_envelope: what the MAC covers
# ---------------------------------------------------------------------------


def _resent(env: Envelope, **envelope_overrides: Any) -> Envelope:
    """*env* as the agent would receive it after a tamper of top-level keys in flight."""
    return Envelope.from_json(json.dumps({**env.to_dict(), **envelope_overrides}))


@pytest.mark.parametrize(
    "key, value", [("id", "forged-id"), ("agent_id", "other-agent")]
)
def test_verify_refuses_a_changed_envelope_key(
    nonce_store: NonceStore, key: str, value: str
) -> None:
    env = _resent(signed_command_request(), **{key: value})
    with pytest.raises(AuthError) as exc_info:
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert exc_info.value.reason == "bad_signature"


def test_verify_refuses_a_changed_nonce(nonce_store: NonceStore) -> None:
    env = signed_command_request()
    env.payload["nonce"] = "other-nonce"
    with pytest.raises(AuthError) as exc_info:
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert exc_info.value.reason == "bad_signature"


def test_verify_refuses_an_envelope_never_received(nonce_store: NonceStore) -> None:
    """One path: the MAC is checked over the frame as parsed, so an Envelope
    built in-process has nothing to verify against and is refused, never
    re-rendered through ``to_dict``."""
    env = envelope(
        MessageType.COMMAND_REQUEST, {"command": "git_pull", "params": {}, "nonce": "n"}
    )
    env.payload["hmac"] = sign_envelope(env.to_dict(), SECRET)
    assert env.received is None
    with pytest.raises(AuthError) as exc_info:
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert exc_info.value.reason == "bad_signature"


def test_verify_signs_the_frame_as_received_not_a_re_rendering(
    nonce_store: NonceStore,
) -> None:
    """The sender spelled ``ts`` with ``+00:00``; ``to_dict`` would re-render it
    as ``Z`` and refuse a valid signature the day either side's spelling moves."""
    ts = datetime.now(UTC).replace(microsecond=0)
    frame = _request_dict(
        ts=ts.isoformat(),
        payload={"command": "git_pull", "params": {}, "nonce": generate_nonce()},
    )
    assert frame["ts"].endswith("+00:00")
    frame["payload"]["hmac"] = sign_envelope(frame, SECRET)
    env = Envelope.from_json(json.dumps(frame))
    assert env.to_dict()["ts"].endswith("Z")
    payload = verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert isinstance(payload, CommandRequestPayload)


def test_verify_accepts_a_frame_whose_keys_arrive_in_another_order(
    nonce_store: NonceStore,
) -> None:
    env = signed_command_request()
    shuffled = dict(reversed(list(env.to_dict().items())))
    received = Envelope.from_json(json.dumps(shuffled))
    assert isinstance(
        verify_envelope(received, SECRET, nonce_store, max_age_seconds=60),
        CommandRequestPayload,
    )


def test_verify_refuses_an_unsignable_frame_as_bad_signature(
    nonce_store: NonceStore,
) -> None:
    """``NaN`` parses from JSON but no signer can cover it: refused, never a crash."""
    frame = _request_dict(
        ts=datetime.now(UTC).isoformat(),
        payload={
            "command": "git_pull",
            "params": {"x": float("nan")},
            "nonce": "n",
            "hmac": "0" * 64,
        },
    )
    env = Envelope.from_json(json.dumps(frame))
    with pytest.raises(AuthError) as exc_info:
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert exc_info.value.reason == "bad_signature"


def test_verify_refuses_a_non_string_hmac(nonce_store: NonceStore) -> None:
    env = signed_command_request()
    env.payload["hmac"] = 12345
    with pytest.raises(AuthError) as exc_info:
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert exc_info.value.reason == "bad_signature"


@pytest.mark.parametrize("where", ["envelope", "payload"])
def test_verify_refuses_a_key_added_in_flight(
    nonce_store: NonceStore, where: str
) -> None:
    """An additive field is signed for free (CORE-011 d4): one the sender never
    signed, at either level, is a forgery, not a harmless extra."""
    frame = signed_command_request().to_dict()
    if where == "envelope":
        frame["extra"] = "x"
    else:
        frame["payload"] = {**frame["payload"], "extra": "x"}
    env = Envelope.from_json(json.dumps(frame))
    with pytest.raises(AuthError) as exc_info:
        verify_envelope(env, SECRET, nonce_store, max_age_seconds=60)
    assert exc_info.value.reason == "bad_signature"


def test_sign_envelope_bytes_are_the_canonical_json_the_control_plane_must_write() -> (
    None
):
    """The signed bytes, spelled out by hand: keys sorted at every level, compact,
    non-ASCII escaped, ``payload.hmac`` absent. The hex is a literal, not a
    rendering, so a signer that drifts cannot regenerate its way back to green."""
    key = bytes(range(32))
    frame = {
        "v": 2,
        "type": "command.request",
        "id": "00000000-0000-4000-8000-00000000000a",
        "ts": "2026-10-09T00:00:00Z",
        "agent_id": "vector-agent",
        "payload": {
            "hmac": "ignored",
            "nonce": "n-1",
            "params": {"tail": "50", "service": "café"},
            "command": "docker_logs",
        },
    }
    canonical = (
        b'{"agent_id":"vector-agent","id":"00000000-0000-4000-8000-00000000000a",'
        b'"payload":{"command":"docker_logs","nonce":"n-1",'
        b'"params":{"service":"caf\\u00e9","tail":"50"}},'
        b'"ts":"2026-10-09T00:00:00Z","type":"command.request","v":2}'
    )
    expected = "7e3bd0f160202e57a53605329d774fde598dfbd7123f8e34e86ddc8c9803fbc6"
    assert hmac.new(key, canonical, hashlib.sha256).hexdigest() == expected
    assert sign_envelope(frame, key) == expected
