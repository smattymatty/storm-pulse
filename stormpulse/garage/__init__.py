"""Garage S3 node integration - discovery, state collection, and commands."""

from stormpulse.garage.bucket_resolver import BucketIdResolver
from stormpulse.garage.commands import build_garage_specs
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
    "build_garage_specs",
    "collect_garage_state",
    "discover_garage",
]
