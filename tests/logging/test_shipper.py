"""Tests for LogShipper."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from stormpulse.config import LogGroupConfig
from stormpulse.logging.positions import LogPositionStore
from stormpulse.logging.shipper import LogShipper
from stormpulse.logging.tailer import DockerTailer, LogTailer


def _make_group(
    source_path: Path,
    *,
    parser: str = "stormpulse",
    filter_contains: str = "",
    max_batch: int = 50,
) -> LogGroupConfig:
    return LogGroupConfig(
        name="test",
        enabled=True,
        source_type="file",
        source_path=source_path,
        filter_contains=filter_contains,
        parser=parser,
        ship_interval_seconds=10.0,
        max_lines_per_batch=max_batch,
        retention_days=30,
    )


def _stormpulse_line(message: str) -> str:
    return json.dumps(
        {
            "ts": "2026-04-10T13:00:00Z",
            "level": "INFO",
            "message": message,
            "event_type": "connection",
        }
    )


def test_empty_source_returns_none(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "t.log"
    log.touch()
    group = _make_group(log)
    shipper = LogShipper(group, LogTailer(group, store))
    assert shipper.collect_batch() is None
    store.close()


def test_parses_valid_lines(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "t.log"
    log.write_text(_stormpulse_line("a") + "\n" + _stormpulse_line("b") + "\n")
    group = _make_group(log)
    shipper = LogShipper(group, LogTailer(group, store))
    batch = shipper.collect_batch()
    assert batch is not None
    assert len(batch.lines) == 2
    assert batch.dropped == 0
    assert batch.from_position == 0
    assert isinstance(batch.to_position, int)
    assert batch.to_position > 0
    store.close()


def test_malformed_lines_counted_as_dropped(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "t.log"
    log.write_text(
        _stormpulse_line("ok") + "\nnot json\n" + _stormpulse_line("ok2") + "\n"
    )
    group = _make_group(log)
    shipper = LogShipper(group, LogTailer(group, store))
    batch = shipper.collect_batch()
    assert batch is not None
    assert len(batch.lines) == 2
    assert batch.dropped == 1
    store.close()


def test_filter_contains_skips_non_matching(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "t.log"
    log.write_text(
        _stormpulse_line("keep this") + "\n" + _stormpulse_line("drop this") + "\n"
    )
    group = _make_group(log, filter_contains="keep")
    shipper = LogShipper(group, LogTailer(group, store))
    batch = shipper.collect_batch()
    assert batch is not None
    assert len(batch.lines) == 1
    # Skipped lines are NOT counted as dropped (they're just not for us)
    assert batch.dropped == 0
    store.close()


def test_max_batch_limit_respected(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "t.log"
    log.write_text("".join(_stormpulse_line(f"m{i}") + "\n" for i in range(10)))
    group = _make_group(log, max_batch=3)
    shipper = LogShipper(group, LogTailer(group, store))
    batch = shipper.collect_batch()
    assert batch is not None
    assert len(batch.lines) == 3
    # Remaining lines should be available on next call after confirm
    assert isinstance(batch.to_position, int)
    shipper.tailer.confirm_shipped(batch.to_position)  # type: ignore[arg-type]
    batch2 = shipper.collect_batch()
    assert batch2 is not None
    assert len(batch2.lines) == 3
    store.close()


def test_unknown_parser_raises(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "t.log"
    group = LogGroupConfig(
        name="x",
        enabled=True,
        source_type="file",
        source_path=log,
        filter_contains="",
        parser="not_a_parser",
        ship_interval_seconds=10.0,
        max_lines_per_batch=5,
        retention_days=1,
    )
    with pytest.raises(ValueError):
        LogShipper(group, LogTailer(group, store))
    store.close()


def test_position_not_advanced_without_confirm(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "t.log"
    log.write_text(_stormpulse_line("a") + "\n")
    group = _make_group(log)
    shipper = LogShipper(group, LogTailer(group, store))
    batch1 = shipper.collect_batch()
    assert batch1 is not None
    # Without confirm, position in store is still 0
    assert store.get("test") == (0, None)
    # Re-reading returns the same lines again
    batch2 = shipper.collect_batch()
    assert batch2 is not None
    assert len(batch2.lines) == len(batch1.lines)
    store.close()


def test_all_filtered_returns_none(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "t.log"
    log.write_text(_stormpulse_line("irrelevant") + "\n")
    group = _make_group(log, filter_contains="needle")
    shipper = LogShipper(group, LogTailer(group, store))
    assert shipper.collect_batch() is None
    store.close()


def test_all_dropped_ships_drop_count(tmp_path: Path) -> None:
    """Unparseable source still ships dropped count so dashboard sees the signal."""
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "t.log"
    log.write_text("bad\nbad\nbad\n")
    group = _make_group(log)
    shipper = LogShipper(group, LogTailer(group, store))
    batch = shipper.collect_batch()
    assert batch is not None
    assert batch.lines == []
    assert batch.dropped == 3
    store.close()


def test_shipper_with_docker_tailer(tmp_path: Path) -> None:
    """Confirm LogShipper works when handed a DockerTailer - to_position
    is a string timestamp (not int) and confirm_shipped accepts it."""
    store = LogPositionStore(tmp_path / "pos.db")
    group = LogGroupConfig(
        name="web",
        enabled=True,
        source_type="docker",
        source_path=Path(""),
        filter_contains="",
        parser="docker_raw",
        ship_interval_seconds=10.0,
        max_lines_per_batch=50,
        retention_days=30,
        container_name="web",
        docker_binary="/usr/bin/docker",
    )
    store.set_docker_ts("web", "web", "2026-04-16T13:00:00.000000Z")
    tailer = DockerTailer(group, store)
    shipper = LogShipper(group, tailer)

    stdout = "2026-04-16T13:00:01.000000Z some log line\n"
    with patch("stormpulse.logging.tailer.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=stdout,
            stderr="",
        )
        batch = shipper.collect_batch()

    assert batch is not None
    assert len(batch.lines) == 1
    assert isinstance(batch.to_position, str)
    assert batch.to_position > "2026-04-16T13:00:01.000000Z"
    # Confirm round-trips through the store cleanly
    shipper.tailer.confirm_shipped(batch.to_position)  # type: ignore[arg-type]
    assert store.get_docker_ts("web") == batch.to_position
    store.close()


# ---------------------------------------------------------------------------
# bucket_id stamping
# ---------------------------------------------------------------------------

from stormpulse.garage.state import GarageBucket, GarageKeyRef, GarageState  # noqa: E402
from stormpulse.garage.bucket_resolver import BucketIdResolver  # noqa: E402

_GARAGE_LINE = (
    "2026-04-10T13:23:51.766230Z  INFO garage_api_common::generic_server: "
    "71.19.243.102 (via [::1]:37780) (key {key}) HEAD /{bucket}\n"
)


def _garage_group(source_path: Path) -> LogGroupConfig:
    return _make_group(source_path, parser="garage_s3")


def _resolver_for(key_id: str, bucket_name: str, bucket_id: str) -> BucketIdResolver:
    state = GarageState(
        node_id="n1", hostname="h", zone="z", capacity_gb=1.0, data_avail_gb=1.0,
        version="v", healthy=True, object_count=0, keys=[], peers=[],
        buckets=[
            GarageBucket(
                id=bucket_id, alias="", size_bytes=0, object_count=0,
                keys=[GarageKeyRef(
                    key_id=key_id, key_name="k", permissions="RWO",
                    bucket_local_aliases=(bucket_name,),
                )],
                website_access=False, website_index_document="index.html",
                website_error_document=None, quota_max_size_bytes=None,
                quota_max_objects=None,
            ),
        ],
    )
    return BucketIdResolver.from_state(state)


def test_garage_s3_line_stamped_with_resolved_bucket_id(tmp_path: Path) -> None:
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "garage.log"
    log.write_text(_GARAGE_LINE.format(key="GKaccount01", bucket="media"))
    group = _garage_group(log)
    shipper = LogShipper(group, LogTailer(group, store))
    resolver = _resolver_for("GKaccount01", "media", "bid-media-000001")

    batch = shipper.collect_batch(resolver)
    assert batch is not None
    assert len(batch.lines) == 1
    assert batch.lines[0]["bucket_id"] == "bid-media-000001"
    store.close()


def test_garage_s3_line_unresolved_gets_empty_bucket_id(tmp_path: Path) -> None:
    # Brand-new bucket not in the last state snapshot -> '' (website falls
    # back to key-anchoring).
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "garage.log"
    log.write_text(_GARAGE_LINE.format(key="GKaccount01", bucket="brand-new"))
    group = _garage_group(log)
    shipper = LogShipper(group, LogTailer(group, store))
    resolver = _resolver_for("GKaccount01", "media", "bid-media-000001")

    batch = shipper.collect_batch(resolver)
    assert batch is not None
    assert batch.lines[0]["bucket_id"] == ""
    store.close()


def test_garage_s3_no_resolver_leaves_field_off(tmp_path: Path) -> None:
    # Backward-compatible: collect_batch() with no resolver ships no bucket_id.
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "garage.log"
    log.write_text(_GARAGE_LINE.format(key="GKaccount01", bucket="media"))
    group = _garage_group(log)
    shipper = LogShipper(group, LogTailer(group, store))

    batch = shipper.collect_batch()
    assert batch is not None
    assert "bucket_id" not in batch.lines[0]
    store.close()


def test_non_garage_group_ignores_resolver(tmp_path: Path) -> None:
    # A resolver passed to a non-garage_s3 group never stamps bucket_id.
    store = LogPositionStore(tmp_path / "pos.db")
    log = tmp_path / "t.log"
    log.write_text(_stormpulse_line("a") + "\n")
    group = _make_group(log)  # parser="stormpulse"
    shipper = LogShipper(group, LogTailer(group, store))
    resolver = _resolver_for("GKaccount01", "media", "bid-media-000001")

    batch = shipper.collect_batch(resolver)
    assert batch is not None
    assert "bucket_id" not in batch.lines[0]
    store.close()
