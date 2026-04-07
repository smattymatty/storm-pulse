"""Garage S3 node integration — discovery, state collection, and commands."""

from stormpulse.garage.commands import build_garage_commands
from stormpulse.garage.discover import discover_garage
from stormpulse.garage.state import (
    GarageBucket,
    GarageKeyRef,
    GarageState,
    collect_garage_state,
)

__all__ = [
    "GarageBucket",
    "GarageKeyRef",
    "GarageState",
    "build_garage_commands",
    "collect_garage_state",
    "discover_garage",
]
