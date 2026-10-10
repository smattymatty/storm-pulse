"""The golden MAC vectors (CORE-011 decision 4): signed with the published key,
fixed so regeneration is a no-op, and outside both digests.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fitness.wire_contract import WIRE_CONTRACT_PATH
from stormpulse.agent.wire_contract import (
    CONTRACT_DIGEST_COVERS,
    VECTOR_SECRET_HEX,
    build_wire_contract,
    contract_digest,
    wire_contract_vectors,
)
from stormpulse.auth import sign_envelope
from stormpulse.protocol import (
    MESSAGES,
    SPEAKS,
    Envelope,
    MessageType,
)
from stormpulse.sdk.declaration import canonical_digest

_SIGNED_TYPES = {MessageType.COMMAND_REQUEST.value, MessageType.COMMAND_SEQUENCE.value}


def test_vectors_sign_with_the_published_key() -> None:
    """A consumer holding only the file reproduces each ``hmac`` from the
    envelope and the key with ``sign_envelope``; the frame itself parses at
    the spoken version into the typed payload it names. Never ``verify_envelope``:
    freshness refuses a fixed ``ts``, which is the point of a vector."""
    vectors = json.loads(WIRE_CONTRACT_PATH.read_text(encoding="utf-8"))["vectors"]
    assert set(vectors) == _SIGNED_TYPES
    for name, vector in vectors.items():
        assert set(vector) == {"secret", "envelope", "hmac"}
        key = bytes.fromhex(vector["secret"])
        assert key == bytes(range(32))
        assert sign_envelope(vector["envelope"], key) == vector["hmac"]
        assert vector["envelope"]["payload"]["hmac"] == vector["hmac"]
        frame = Envelope.from_json(json.dumps(vector["envelope"]))
        assert frame.v == SPEAKS[-1]
        assert frame.type.value == name
        payload = MESSAGES[frame.type].payload.from_dict(frame.payload)
        assert payload.hmac == vector["hmac"]  # type: ignore[attr-defined]


def test_vectors_are_fixed_so_regeneration_is_a_no_op() -> None:
    """No fresh id, ts or nonce: two renders agree byte for byte."""
    assert wire_contract_vectors() == wire_contract_vectors()
    assert VECTOR_SECRET_HEX == bytes(range(32)).hex()


def test_vectors_sit_outside_both_digests() -> None:
    """The digests hash the shape, never an example of it: a vector that moves
    (a new test key, a new fixed nonce) is not a contract change."""
    artifact = build_wire_contract()
    moved = json.loads(json.dumps(artifact))
    for vector in moved["vectors"].values():
        vector["hmac"] = "0" * 64
        vector["envelope"]["payload"]["hmac"] = "0" * 64
    assert "vectors" not in CONTRACT_DIGEST_COVERS
    assert contract_digest(moved) == artifact["contract_digest"]
    assert canonical_digest(moved[moved["digest_covers"]]) == artifact["digest"]


def test_fitness_fails_on_a_hand_edited_or_missing_vector(
    check_with: Callable[[dict[str, Any]], list[str]],
) -> None:
    artifact = build_wire_contract()
    artifact["vectors"]["command.request"]["hmac"] = "0" * 64
    del artifact["vectors"]["command.sequence"]
    violations = check_with(artifact)
    assert (
        "vectors.command.request: declared vector is not what the live signer "
        "writes; regenerate" in "\n".join(violations)
    )
    assert any(
        "vectors: vector 'command.sequence' is signed but not declared" in v
        for v in violations
    )
    assert not any("digest" in v for v in violations)
