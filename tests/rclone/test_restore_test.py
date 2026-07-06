"""Tests for the rclone_restore_test job. The load-bearing assertions:
sample selection (first non-empty, scratch excluded) and scratch cleanup
on both the success and the failure path."""

from __future__ import annotations

import json

import pytest

from stormpulse.commands.jobs import JobOutcome
from stormpulse.rclone.jobs import restore_test
from tests.rclone.helpers import CONFIG, REMOTE, FakeRclone, ProgressRecorder

_LISTING = json.dumps(
    [
        {"Path": ".storm-restore-test/old.bin", "Size": 10, "IsDir": False},
        {"Path": "empty-marker", "Size": 0, "IsDir": False},
        {"Path": "photos/pho[1] {a}.jpg", "Size": 2048, "IsDir": False},
        {"Path": "later.bin", "Size": 4096, "IsDir": False},
    ]
)

_OK = (0, "", "")


def _fake(**overrides: tuple[int, str, str] | Exception) -> FakeRclone:
    replies: dict[str, tuple[int, str, str] | Exception] = {
        "lsjson": (0, _LISTING, ""),
        "copyto": _OK,
        "check": _OK,
        "purge": _OK,
    }
    replies.update(overrides)
    return FakeRclone(replies)


async def _run(fake: FakeRclone, monkeypatch: pytest.MonkeyPatch) -> JobOutcome:
    monkeypatch.setattr(restore_test, "run_rclone", fake)
    return await restore_test.run_restore_test(
        ProgressRecorder(), CONFIG, REMOTE, "storm-bucket"
    )


@pytest.mark.asyncio
async def test_success_samples_first_nonempty_outside_scratch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake()
    outcome: JobOutcome = await _run(fake, monkeypatch)
    assert outcome.success is True
    assert outcome.extras["sample_object"] == "photos/pho[1] {a}.jpg"
    assert outcome.extras["sample_bytes"] == 2048
    # Round trip stays inside the customer's bucket, scratch mirrors the path.
    copy_args = fake.args_for("copyto")
    assert "DST:storm-bucket/photos/pho[1] {a}.jpg" in copy_args
    assert "DST:storm-bucket/.storm-restore-test/photos/pho[1] {a}.jpg" in copy_args
    # Glob metacharacters in the key are escaped in the check filter.
    check_args = fake.args_for("check")
    include = check_args[check_args.index("--include") + 1]
    assert include == "/photos/pho\\[1\\] \\{a\\}.jpg"
    assert "--download" in check_args
    assert fake.called("purge")


@pytest.mark.asyncio
async def test_check_mismatch_fails_named_and_still_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake(check=(1, "", "1 differences found"))
    outcome: JobOutcome = await _run(fake, monkeypatch)
    assert outcome.success is False
    assert outcome.failure_reason == "restore_mismatch"
    assert fake.called("purge")


@pytest.mark.asyncio
async def test_copy_failure_still_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake(copyto=(7, "", "FATAL: denied"))
    outcome: JobOutcome = await _run(fake, monkeypatch)
    assert outcome.success is False
    assert outcome.failure_reason == "fatal_error"
    assert fake.called("purge")


@pytest.mark.asyncio
async def test_failed_cleanup_is_loud_on_the_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake(purge=(7, "", "FATAL: cannot delete"))
    outcome: JobOutcome = await _run(fake, monkeypatch)
    assert outcome.extras["manual_cleanup_required"] == [
        {"type": "prefix", "path": "storm-bucket/.storm-restore-test"},
    ]


@pytest.mark.asyncio
async def test_purge_on_missing_scratch_counts_as_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake(purge=(3, "", "directory not found"))
    outcome: JobOutcome = await _run(fake, monkeypatch)
    assert "manual_cleanup_required" not in outcome.extras


@pytest.mark.asyncio
async def test_all_empty_or_scratch_objects_is_a_named_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    listing = json.dumps(
        [
            {"Path": ".storm-restore-test/old.bin", "Size": 10, "IsDir": False},
            {"Path": "empty-marker", "Size": 0, "IsDir": False},
        ]
    )
    fake = _fake(lsjson=(0, listing, ""))
    outcome: JobOutcome = await _run(fake, monkeypatch)
    assert outcome.success is False
    assert outcome.failure_reason == "no_sample_object"
    # Nothing was written, so nothing to copy or check.
    assert not fake.called("copyto")
    assert not fake.called("check")
