"""Garage S3 node integration - discovery, state collection, and commands."""

from stormpulse.garage.bucket_resolver import BucketIdResolver
from stormpulse.garage.commands import build_garage_commands, long_running_factories
from stormpulse.garage.discover import discover_garage
from stormpulse.garage.state import (
    GarageBucket,
    GarageKeyRef,
    GaragePeer,
    GarageState,
    collect_garage_state,
)

__all__ = [
    "BucketIdResolver",
    "GarageBucket",
    "GarageKeyRef",
    "GaragePeer",
    "GarageState",
    "build_garage_commands",
    "collect_garage_state",
    "discover_garage",
    "long_running_factories",
]
