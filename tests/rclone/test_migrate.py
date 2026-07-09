"""Tests for the rclone_migrate job. The load-bearing assertion: progress
is aggregates only, no per-object name ever leaves the handler."""

from __future__ import annotations

from typing import Any

import pytest

from stormpulse.rclone.jobs import migrate
from stormpulse.rclone.runner import StatsCallback
from tests.rclone.helpers import CONFIG, REMOTE, ProgressRecorder

# A stats object as rclone emits it: the transferring array names in-flight
# files, which must never surface in progress.
_STATS = {
    "bytes": 2048,
    "totalBytes": 4096,
    "transfers": 1,
    "totalTransfers": 2,
    "eta": 30,
    "speed": 1024.5,
    "transferring": [{"name": "clients/jane-doe-tax.pdf", "bytes": 1024}],
}


class FakeStreaming:
    """Fake for ``run_rclone_streaming``: feeds stats, returns (code, tail)."""

    def __init__(self, stats: list[dict[str, Any]], code: int, tail: str) -> None:
        self.stats = stats
        self.code = code
        self.tail = tail
        self.args: tuple[str, ...] = ()

    async def __call__(
        self,
        config: Any,
        *args: str,
        env: dict[str, str],
        on_stats: StatsCallback,
    ) -> tuple[int, str]:
        self.args = args
        for entry in self.stats:
            await on_stats(entry)
        return (self.code, self.tail)


async def _run(fake: FakeStreaming, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ProgressRecorder]:
    monkeypatch.setattr(migrate, "run_rclone_streaming", fake)
    progress = ProgressRecorder()
    outcome = await migrate.run_migrate(
        progress, CONFIG, REMOTE, "their-bucket", REMOTE, "storm-bucket"
    )
    return outcome, progress


@pytest.mark.asyncio
async def test_success_reports_aggregates(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeStreaming([_STATS], code=0, tail="")
    outcome, progress = await _run(fake, monkeypatch)
    assert outcome.success is True
    assert outcome.extras["bytes_transferred"] == 2048
    assert outcome.extras["objects_transferred"] == 1
    assert ("running", 2048, 4096, "1 of 2 objects, ETA 30s") in progress.events
    assert "SRC:their-bucket" in fake.args and "DST:storm-bucket" in fake.args


@pytest.mark.asyncio
async def test_progress_carries_no_object_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeStreaming([_STATS], code=0, tail="")
    _, progress = await _run(fake, monkeypatch)
    for _, _, _, message in progress.events:
        assert "jane-doe" not in message
        assert ".pdf" not in message
    # The structured channel must not become a second leak path. TransferStats
    # is aggregates-only by construction; assert nothing named slipped in.
    for transfer in progress.transfers:
        assert "jane-doe" not in repr(transfer)
        assert ".pdf" not in repr(transfer)


@pytest.mark.asyncio
async def test_running_progress_carries_structured_transfer_stats(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consumer's live readout (rate, ETA, object counts) is fed from these
    structured fields, never parsed back out of the ``message`` string."""
    fake = FakeStreaming([_STATS], code=0, tail="")
    _, progress = await _run(fake, monkeypatch)

    running = [t for (stage, *_), t in zip(progress.events, progress.transfers) if stage == "running"]
    assert len(running) == 1
    transfer = running[0]
    assert transfer is not None
    assert transfer.rate_bytes_per_sec == 1024  # float speed coerced down
    assert transfer.objects_current == 1
    assert transfer.objects_total == 2
    assert transfer.eta_seconds == 30


@pytest.mark.asyncio
async def test_the_starting_frame_has_no_transfer_stats_yet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeStreaming([_STATS], code=0, tail="")
    _, progress = await _run(fake, monkeypatch)
    starting = next(
        t for (stage, *_), t in zip(progress.events, progress.transfers)
        if stage == "starting"
    )
    assert starting is None


@pytest.mark.asyncio
async def test_missing_speed_and_null_eta_degrade_rather_than_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rclone omits ``speed`` and reports a null ``eta`` until it has samples.
    A stats line must never be able to abort a transfer in progress."""
    stats = {**_STATS, "eta": None}
    del stats["speed"]
    fake = FakeStreaming([stats], code=0, tail="")
    outcome, progress = await _run(fake, monkeypatch)

    assert outcome.success is True
    transfer = progress.transfers[-1]
    assert transfer is not None
    assert transfer.rate_bytes_per_sec == 0
    assert transfer.eta_seconds is None


@pytest.mark.asyncio
async def test_nothing_to_transfer_is_a_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeStreaming([], code=9, tail="")
    outcome, _ = await _run(fake, monkeypatch)
    assert outcome.success is True
    assert "already current" in outcome.stdout


@pytest.mark.asyncio
async def test_failure_names_reason_and_keeps_partial_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeStreaming([_STATS], code=7, tail="FATAL: connection refused")
    outcome, _ = await _run(fake, monkeypatch)
    assert outcome.success is False
    assert outcome.failure_reason == "fatal_error"
    assert outcome.stderr == "FATAL: connection refused"
    assert outcome.extras["bytes_transferred"] == 2048


def test_handler_refuses_missing_destination() -> None:
    params = {
        "src_endpoint": "https://s3.source.example",
        "src_region": "us-east-1",
        "src_bucket": "their-bucket",
        "src_access_key_id": "AKIAEXAMPLE",
        "src_secret_access_key": "sourcesecret",
    }
    assert migrate.make_migrate_handler(CONFIG, params) is None
