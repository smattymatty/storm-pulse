"""Shared fakes for the GarageStateReader tests: a config, a hand-moved clock,
and the five admin reads patched with counting mocks."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from stormpulse.garage.config import GarageConfig

ADMIN_URL = "http://127.0.0.1:3903"
FULL_ID = "f1dc32249aa1d80a" + "0" * 48
ADMIN_TOKEN = "tok"  # skylos: ignore[SKY-L014] fake token, every admin call is mocked
NODE_ID = "a8bfb94f8a2786f74c227c75a690846b915560c08dc8a0c8681b980082d0a4b9"


def reader_config(
    *,
    admin_url: str = ADMIN_URL,
    admin_token: str = ADMIN_TOKEN,
    hint_file: str = "/nonexistent/hint.json",
) -> GarageConfig:
    return GarageConfig(
        enabled=True,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=Path("/tmp/garage.toml"),
        admin_url=admin_url,
        admin_token=admin_token,
        hint_file=hint_file,
    )


class Clock:
    """Injectable monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def node() -> dict[str, Any]:
    return {
        "id": NODE_ID,
        "hostname": "garage-one",
        "addr": "10.0.0.1:3901",
        "garageVersion": "v2.3.0",
        "isUp": True,
        "role": {"zone": "canada-1", "capacity": 3_000_000_000_000, "tags": []},
        "dataPartition": {"available": 2_800_000_000_000, "total": 3_000_000_000_000},
    }


def info(bucket_id: str = FULL_ID) -> dict[str, Any]:
    return {
        "id": bucket_id,
        "globalAliases": ["media"],
        "websiteAccess": False,
        "websiteConfig": None,
        "keys": [],
        "objects": 3,
        "bytes": 1024,
        "quotas": {"maxSize": None, "maxObjects": None},
    }


@contextmanager
def patched(
    *,
    status: MagicMock | None = None,
    list_buckets: MagicMock | None = None,
) -> Iterator[dict[str, MagicMock]]:
    """Patch the five admin reads with counting mocks; override status/list_buckets to inject failures."""
    status = status or MagicMock(return_value=({"nodes": [node()]}, ""))
    list_buckets = list_buckets or MagicMock(return_value=([{"id": FULL_ID}], ""))
    stats = MagicMock(return_value=({"totalObjectCount": 5}, ""))
    list_keys = MagicMock(return_value=([{"id": "GKabc", "name": "k"}], ""))
    get_info = MagicMock(side_effect=lambda **kw: (info(kw["bucket_ref"]), ""))
    with (
        patch("stormpulse.garage.state.admin_api.get_cluster_status", status),
        patch("stormpulse.garage.state.admin_api.get_cluster_statistics", stats),
        patch("stormpulse.garage.state.admin_api.list_keys", list_keys),
        patch("stormpulse.garage.state.admin_api.list_buckets", list_buckets),
        patch("stormpulse.garage.state.admin_api.get_bucket_info", get_info),
    ):
        yield {
            "status": status,
            "stats": stats,
            "list_keys": list_keys,
            "list_buckets": list_buckets,
            "get_info": get_info,
        }


def hex_id(i: int) -> str:
    return f"{i:064x}"
