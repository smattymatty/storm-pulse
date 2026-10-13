"""The two digests, their reproducibility, and what each covers (CORE-008).

Written before the implementation, deliberately: if the artifact's ``digest``
is not reproducible from the artifact's own bytes, a consumer that vendors the
file can never reproduce it and its reconcile refuses forever, silently.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from fitness.__main__ import load_baseline
from fitness.wire_contract import WIRE_CONTRACT_PATH, check_wire_contract
from stormpulse.agent.wire_contract import (
    CONTRACT_DIGEST_COVERS,
    SCHEMA,
    build_wire_contract,
    contract_digest,
    render_wire_contract,
    wire_contract_digest,
    wire_contract_integrations,
)
from stormpulse.garage.state import GarageBucket, GarageKeyRef, GarageState
from stormpulse.protocol import (
    dataclass_wire_shape,
    dataclass_wire_types,
)
from stormpulse.sdk.declaration import canonical_digest

# -- the crux: both ends must hash identical bytes ---------------------------


def test_artifact_digest_is_reproducible_from_the_artifact_alone() -> None:
    """A consumer holding ONLY the file can recompute the digest it carries.

    This is the property the whole two-ended design rests on. The consumer has
    no dataclasses, only bytes, so if the carried digest is not a function of
    the carried shape the consumer is locked out permanently.
    """
    artifact = json.loads(WIRE_CONTRACT_PATH.read_text(encoding="utf-8"))
    recomputed = canonical_digest(artifact[artifact["digest_covers"]])
    assert recomputed == artifact["digest"]


def test_artifact_is_self_describing_about_what_the_digest_covers() -> None:
    """``digest_covers`` names a real top-level key, so the recompute above is
    not a convention a consumer has to be told out of band."""
    artifact = json.loads(WIRE_CONTRACT_PATH.read_text(encoding="utf-8"))
    assert artifact["digest_covers"] in artifact


def test_checked_in_artifact_matches_the_live_dataclasses() -> None:
    """Function 9's property, asserted from the test suite too.

    The fitness runner is the gate; this is here so a contributor running
    ``pytest`` alone still learns the artifact is stale. The baseline's own
    lines are the harness's business, subtracted here as it subtracts them.
    """
    assert [v for v in check_wire_contract() if v not in load_baseline()] == []


def test_rendered_artifact_is_byte_identical_to_the_checked_in_file() -> None:
    """Regenerating must be a no-op, or every unrelated commit carries churn."""
    assert render_wire_contract() == WIRE_CONTRACT_PATH.read_text(encoding="utf-8")


# -- the digest is derived from the classes, never from the file -------------


def test_runtime_digest_equals_the_live_shape_hashed() -> None:
    assert wire_contract_digest() == canonical_digest(wire_contract_integrations())


def test_agent_wire_contract_module_cannot_read_the_artifact() -> None:
    """CORE-008 decision 4, enforced structurally rather than by convention.

    A digest read from a file can be stale with respect to the process that
    sends it. This asserts the runtime module has no way to read one: no
    filesystem import, no ``open``, no ``__file__``. Changing the runtime path
    to consult the artifact fails here before it can ship a stale advertisement.
    """
    from stormpulse.agent import wire_contract as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "pathlib" not in imported
    assert "os" not in imported
    assert "io" not in imported

    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "open" not in called
    assert "__file__" not in source


# -- what the shape covers, and what it deliberately does not ----------------


def test_shape_walks_every_dataclass_reachable_from_the_root() -> None:
    classes = wire_contract_integrations()["garage"]["classes"]
    assert set(classes) == {
        "GarageState",
        "GarageBucket",
        "GarageKeyRef",
        "GaragePeer",
        "GarageAdminMetric",
    }


def test_shape_records_nesting_by_class_name() -> None:
    classes = wire_contract_integrations()["garage"]["classes"]
    assert classes["GarageState"]["buckets"] == "GarageBucket"
    assert classes["GarageState"]["peers"] == "GaragePeer"
    assert classes["GarageBucket"]["keys"] == "GarageKeyRef"
    # A tuple of plain strings is a leaf, not a nesting.
    assert classes["GarageKeyRef"]["bucket_local_aliases"] is None


def test_shape_field_names_match_what_asdict_actually_emits() -> None:
    """The artifact must describe the bytes on the wire, not a parallel belief.

    ``GarageState.to_dict`` is ``asdict``, so the emitted JSON keys ARE the
    dataclass field names, recursively. If the walker and ``asdict`` ever
    disagree, the published contract is a lie in exactly the way this unit
    exists to prevent.
    """
    state = GarageState(
        node_id="n1",
        hostname="host",
        zone="dc1",
        capacity_gb=1.0,
        data_avail_gb=1.0,
        version="v2",
        healthy=True,
        object_count=0,
        buckets=[
            GarageBucket(
                id="b1",
                alias="a",
                size_bytes=0,
                object_count=0,
                keys=[GarageKeyRef(key_id="k", key_name="n", permissions="RW")],
                website_access=False,
                website_index_document="index.html",
                website_error_document=None,
                quota_max_size_bytes=None,
                quota_max_objects=None,
            )
        ],
        keys=[],
        peers=[],
    )
    emitted = state.to_dict()
    classes = wire_contract_integrations()["garage"]["classes"]

    assert set(emitted) == set(classes["GarageState"])
    assert set(emitted["buckets"][0]) == set(classes["GarageBucket"])
    assert set(emitted["buckets"][0]["keys"][0]) == set(classes["GarageKeyRef"])


def test_digest_ignores_types_and_defaults_but_not_names() -> None:
    """CORE-008 decision 5, stated as a test: names and nesting, nothing else.

    A consumer that reads an unchanged digest as proof of semantic stability
    has misread it, so the scope had better be exactly what the ADR claims.
    """

    @dataclass(frozen=True)
    class Before:
        count: int
        label: str

    @dataclass(frozen=True)
    class Retyped:
        count: str  # units/type changed, meaning changed, names did not
        label: str

    @dataclass(frozen=True)
    class Renamed:
        total: int  # a rename
        label: str

    before = dataclass_wire_shape(Before)["Before"]
    # A type change is invisible to the digest, and saying so is the decision.
    assert before == dataclass_wire_shape(Retyped)["Retyped"]
    assert canonical_digest(before) == canonical_digest(
        dataclass_wire_shape(Retyped)["Retyped"]
    )
    # A rename is not.
    assert before != dataclass_wire_shape(Renamed)["Renamed"]
    assert canonical_digest(before) != canonical_digest(
        dataclass_wire_shape(Renamed)["Renamed"]
    )


def test_walker_refuses_a_non_dataclass_root() -> None:
    with pytest.raises(TypeError):
        dataclass_wire_shape(int)
    with pytest.raises(TypeError, match="dataclass_wire_types requires a dataclass"):
        dataclass_wire_types(int)


def test_read_at_is_declared_and_freshness_stays_off_the_blob() -> None:
    """CORE-011 d5: a bucket's ``read_at`` is part of the blob the digest covers;
    the read's own freshness rides ``IntegrationReport`` and is not declared twice."""
    classes = wire_contract_integrations()["garage"]["classes"]
    assert "read_at" in classes["GarageBucket"]
    assert not {"complete", "last_full_read_at", "changed_ids"} & set(
        classes["GarageState"]
    )


# -- the artifact's own shape ------------------------------------------------


def test_artifact_carries_no_timestamp() -> None:
    """Deliberate: a generated-at stamp would churn the file on every
    regeneration and force Function 9 to ignore a field, which is how a
    contract check learns to ignore things."""
    rendered = render_wire_contract()
    assert "generated" not in rendered
    assert "timestamp" not in rendered


def test_artifact_top_level_keys_are_stable() -> None:
    """A new top-level key is a consumer-breaking event and bumps SCHEMA with it."""
    assert set(build_wire_contract()) == {
        "schema",
        "digest",
        "digest_covers",
        "integrations",
        "messages",
        "commands",
        "contract_digest",
        "vectors",
        "integration_events",
    }
    assert SCHEMA == 5


# -- the v2 contract digest (CORE-011 decision 3) and what sits outside it ---


def test_contract_digest_is_reproducible_from_the_artifact_alone() -> None:
    """The v2 crux, same as the v1 one: a consumer holding only the file
    recomputes ``contract_digest`` from the three sections it covers."""
    artifact = json.loads(WIRE_CONTRACT_PATH.read_text(encoding="utf-8"))
    assert CONTRACT_DIGEST_COVERS == ("integrations", "messages", "commands")
    covered = {key: artifact[key] for key in CONTRACT_DIGEST_COVERS}
    assert canonical_digest(covered) == artifact["contract_digest"]
    assert contract_digest(artifact) == artifact["contract_digest"]
    # And the live classes, never the file, produce the same value.
    assert contract_digest() == artifact["contract_digest"]


def test_contract_digest_moves_with_messages_but_the_v1_digest_does_not() -> None:
    artifact = build_wire_contract()
    renamed = json.loads(json.dumps(artifact))
    classes = renamed["messages"]["command.progress"]["classes"]
    classes["CommandProgressPayload"]["renamed_field"] = classes[
        "CommandProgressPayload"
    ].pop("bytes_freed")
    assert contract_digest(renamed) != artifact["contract_digest"]
    assert canonical_digest(renamed[renamed["digest_covers"]]) == artifact["digest"]


def test_sources_ride_inside_the_contract_digest_but_not_the_v1_digest() -> None:
    artifact = build_wire_contract()
    stripped = json.loads(json.dumps(artifact))
    del stripped["messages"]["events.batch"]["sources"]
    assert contract_digest(stripped) != artifact["contract_digest"]
    assert canonical_digest(stripped[stripped["digest_covers"]]) == artifact["digest"]


# The digest deployed agents advertise and consumers compare. Pinned literally:
# the commands section must never move it, and a deliberate move is a fleet event.
_INTEGRATIONS_DIGEST = (
    "sha256:2a36d610664c613cacf6fae9d14e38caf718601449aed32a1931419a7ece9dd6"
)


def test_commands_are_outside_the_digest() -> None:
    artifact = build_wire_contract()
    assert artifact["digest_covers"] == "integrations"
    assert artifact["digest"] == _INTEGRATIONS_DIGEST
    assert wire_contract_digest() == _INTEGRATIONS_DIGEST


# -- Function 9 on the two digests -------------------------------------------


def test_fitness_fails_on_a_hand_edited_contract_digest(
    check_with: Callable[[dict[str, Any]], list[str]],
) -> None:
    """The brief's second prediction: the shape agrees, the v2 digest does not,
    and the file alone cannot reproduce what it carries."""
    artifact = build_wire_contract()
    live = artifact["contract_digest"]
    artifact["contract_digest"] = "sha256:" + "0" * 64
    violations = check_with(artifact)
    assert any(
        "carries a contract_digest that does not match "
        "('integrations', 'messages', 'commands'), the content it covers, so a "
        "consumer holding only this file cannot reproduce it" in v
        for v in violations
    )
    assert any(
        f"declared contract_digest sha256:{'0' * 64} != live contract_digest {live}"
        in v
        for v in violations
    )
    assert not any("digest_covers" in v or "declared digest " in v for v in violations)


def test_fitness_names_a_wrong_digest_covers_and_still_reproduces_the_digest(
    check_with: Callable[[dict[str, Any]], list[str]],
) -> None:
    """The name is its own line; the digest is checked against the section it
    really covers, so a mislabelled but correct digest reports once."""
    artifact = build_wire_contract()
    artifact["digest_covers"] = "commands"
    violations = check_with(artifact)
    assert "digest_covers is 'commands', expected 'integrations'" in "\n".join(
        violations
    )
    assert not any(
        "carries a digest" in v or "declared digest " in v for v in violations
    )


def test_fitness_keeps_the_v1_digest_lines_apart_from_the_v2_ones(
    check_with: Callable[[dict[str, Any]], list[str]],
) -> None:
    """A hand-edited v1 ``digest`` reports its own two lines and leaves
    ``contract_digest`` silent: the overlap's two digests fail independently."""
    artifact = build_wire_contract()
    artifact["digest"] = "sha256:" + "0" * 64
    violations = check_with(artifact)
    assert sum("carries a digest that does not match" in v for v in violations) == 1
    assert sum(v.startswith("declared digest ") for v in violations) == 1
    assert not any("contract_digest" in v for v in violations)
