"""The declared wire contract this agent emits, and the digest it advertises.

Every Integration that publishes a ``state`` blob declares its shape in its own
package; this module is the one place that can see all of them at once, because
sibling Integrations may not import each other (CORE-000). It assembles them
into the artifact published at the repo root and digests the result.

**This module never touches the filesystem, and that is load-bearing.** The
digest the agent advertises is derived from the live classes in the running
process (CORE-008 decision 4). A digest read from a file can be stale with
respect to the process that sends it, and an advertisement that is not true of
the sender is worse than none: a consumer would trust it. Reading the artifact
is the job of the generator and the fitness check, both of which run in a
checkout, never on a host. ``tests/test_wire_contract.py`` asserts this module
has no way to open a file, so restoring one fails the suite rather than shipping
a stale advertisement.
"""

from __future__ import annotations

import json
from typing import Any

from stormpulse.garage.wire_shape import garage_wire_shape
from stormpulse.sdk.declaration import canonical_digest

# Version of the ARTIFACT's own envelope, not of the shape it carries. Bumped
# only if the file's top-level keys change, which is a consumer-breaking event.
# The shape changing is what the digest is for; this is deliberately not it.
SCHEMA = 1

# Which top-level key the digest covers, carried IN the artifact so a consumer
# holding only the file can reproduce the digest without being told the rule out
# of band. Everything outside it (the schema number, the digest itself) is
# envelope, and an envelope change must not read as a shape change.
DIGEST_COVERS = "integrations"


def wire_contract_integrations() -> dict[str, Any]:
    """Every publishing Integration's declared shape, keyed by integration id.

    One entry today. A second Integration that starts publishing a state blob
    adds itself here, which is the point of the map: its arrival changes the
    digest, and a consumer finds out at the next connect.
    """
    return {"garage": garage_wire_shape()}


def wire_contract_digest() -> str:
    """The digest this process advertises on register.

    True of the agent that sent it by construction: it hashes the classes that
    are about to do the serializing, in this interpreter, right now.
    """
    return canonical_digest(wire_contract_integrations())


def build_wire_contract() -> dict[str, Any]:
    """The full artifact, digest included."""
    integrations = wire_contract_integrations()
    return {
        "schema": SCHEMA,
        "digest": canonical_digest(integrations),
        "digest_covers": DIGEST_COVERS,
        "integrations": integrations,
    }


def render_wire_contract() -> str:
    """The artifact as the exact text the checked-in file holds.

    Indented and key-sorted so a rename lands in review as a readable diff to a
    contract, which is the entire consequence CORE-008 is buying. Carries no
    timestamp: a generated-at stamp would churn the file on every regeneration
    and force the fitness check to learn to ignore a field.
    """
    return json.dumps(build_wire_contract(), indent=2, sort_keys=True, ensure_ascii=True) + "\n"
