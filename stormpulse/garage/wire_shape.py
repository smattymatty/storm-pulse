"""The shape of the state blob garage publishes (CORE-008).

``GarageState`` is serialized by ``asdict`` and pushed as an Integration's
``state``, so every attribute name in this package's dataclass tree is an
interface whether or not anyone declared it one. This module declares it.

Derived from the live classes on every call, never from a file: the whole point
is that the declaration cannot disagree with the process that made it. The
checked-in artifact is a published copy of what this returns, and the fitness
suite is what stops the copy drifting.
"""

from __future__ import annotations

from typing import Any

from stormpulse.garage.state import GarageState
from stormpulse.protocol import dataclass_wire_shape

# The dataclass the state blob serializes from. Every other class in the
# declared shape is reached from here, so this name is the entry point a
# consumer reads first.
ROOT = "GarageState"


def garage_wire_shape() -> dict[str, Any]:
    """Garage's declared emitted shape: the root class name plus every class
    reachable from it, each mapped to its field names and their nesting."""
    return {"root": ROOT, "classes": dataclass_wire_shape(GarageState)}
