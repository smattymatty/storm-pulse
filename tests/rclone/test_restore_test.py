"""Tests for the rclone_restore_test job. The load-bearing assertions:
segmented sample selection (largest + smallest + one per top-level
folder, capped, scratch excluded, empties skipped), the full sample set
round-tripped and checked in one pass, and scratch cleanup on both the
success and the failure path."""

from __future__ import annotations

import json
from typing import Any

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


def _copyto_calls(fake: FakeRclone) -> list[tuple[str, ...]]:
    return [args for args, _ in fake.calls if args[0] == "copyto"]


# ---------------------------------------------------------------------------
# Selection: pure function, no rclone
# ---------------------------------------------------------------------------


def _entries(*pairs: tuple[str, int]) -> list[dict[str, Any]]:
    return [{"Path": p, "Size": s, "IsDir": False} for p, s in pairs]


def test_selects_largest_smallest_and_one_per_folder() -> None:
    samples = restore_test.select_samples(_entries(
        ("backups/full.tar.gz", 5_000_000),
        ("backups/tiny.meta", 12),
        ("photos/a.jpg", 2048),
        ("photos/b.jpg", 4096),
        ("notes.txt", 100),
    ))
    assert samples == [
        ("backups/full.tar.gz", 5_000_000, "largest"),
        ("backups/tiny.meta", 12, "smallest"),
        # backups' folder pick (tiny.meta) deduped into "smallest" above;
        # photos contributes its smallest object.
        ("photos/a.jpg", 2048, "prefix_sample"),
    ]


def test_reason_priority_dedupes_a_double_selected_object() -> None:
    # One folder, one object: largest == smallest == the folder's sample.
    samples = restore_test.select_samples(_entries(("docs/only.pdf", 500)))
    assert samples == [("docs/only.pdf", 500, "largest")]


def test_root_objects_never_create_a_folder_pick() -> None:
    samples = restore_test.select_samples(_entries(
        ("root-a.bin", 10), ("root-b.bin", 20),
    ))
    assert [s[2] for s in samples] == ["largest", "smallest"]


def test_folder_samples_are_capped() -> None:
    many = [(f"dir{i:03d}/obj.bin", 100 + i) for i in range(50)]
    samples = restore_test.select_samples(_entries(*many))
    assert len(samples) == restore_test.MAX_SAMPLES
    # Largest and smallest always survive the cap.
    reasons = [s[2] for s in samples]
    assert reasons[0] == "largest"
    assert reasons[1] == "smallest"
    assert set(reasons[2:]) == {"prefix_sample"}


def test_empty_objects_and_scratch_prefix_are_never_sampled() -> None:
    samples = restore_test.select_samples(_entries(
        (".storm-restore-test/old.bin", 10),
        ("empty-marker", 0),
    ))
    assert samples == []


# ---------------------------------------------------------------------------
# The full job flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_round_trips_the_whole_segmented_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake()
    outcome: JobOutcome = await _run(fake, monkeypatch)
    assert outcome.success is True
    # The receipt names every object, its size, and why it was selected.
    assert outcome.extras["samples"] == [
        {"key": "later.bin", "bytes": 4096, "reason": "largest"},
        {"key": "photos/pho[1] {a}.jpg", "bytes": 2048, "reason": "smallest"},
    ]
    # Back-compat single-object shape stays, pointed at the first sample.
    assert outcome.extras["sample_object"] == "later.bin"
    assert outcome.extras["sample_bytes"] == 4096
    # Every sample round-trips inside the customer's bucket; scratch
    # mirrors each path.
    copies = _copyto_calls(fake)
    assert len(copies) == 2
    assert "DST:storm-bucket/later.bin" in copies[0]
    assert "DST:storm-bucket/.storm-restore-test/later.bin" in copies[0]
    assert "DST:storm-bucket/photos/pho[1] {a}.jpg" in copies[1]
    # One check covers the whole set; glob metacharacters in keys are
    # escaped in its filters.
    check_args = fake.args_for("check")
    includes = [
        check_args[i + 1]
        for i, a in enumerate(check_args)
        if a == "--include"
    ]
    assert includes == ["/later.bin", "/photos/pho\\[1\\] \\{a\\}.jpg"]
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
