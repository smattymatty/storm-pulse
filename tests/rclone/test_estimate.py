"""Tests for the rclone_estimate job."""

from __future__ import annotations

import pytest

from stormpulse.rclone.jobs import estimate
from tests.rclone.helpers import CONFIG, REMOTE, FakeRclone, ProgressRecorder


@pytest.mark.asyncio
async def test_success_reports_bytes_and_objects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRclone({"size": (0, '{"count":42,"bytes":123456,"sizeless":0}', "")})
    monkeypatch.setattr(estimate, "run_rclone", fake)
    outcome = await estimate.run_estimate(
        ProgressRecorder(), CONFIG, REMOTE, "their-bucket"
    )
    assert outcome.success is True
    assert outcome.extras["bytes"] == 123456
    assert outcome.extras["objects"] == 42
    assert "duration_seconds" in outcome.extras
    args = fake.args_for("size")
    assert "SRC:their-bucket" in args and "--json" in args


@pytest.mark.asyncio
async def test_rclone_failure_names_reason_and_caps_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRclone({"size": (3, "", "directory not found")})
    monkeypatch.setattr(estimate, "run_rclone", fake)
    outcome = await estimate.run_estimate(
        ProgressRecorder(), CONFIG, REMOTE, "their-bucket"
    )
    assert outcome.success is False
    assert outcome.failure_reason == "path_not_found"
    assert "directory not found" in outcome.stderr
    # The raw rclone exit rides as the error's upstream code (CORE-011 d9).
    error = outcome.finer_error()
    assert error is not None
    assert (error.upstream_code, error.code) == ("3", "path_not_found")


@pytest.mark.asyncio
async def test_unparseable_json_is_a_named_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeRclone({"size": (0, "not json", "")})
    monkeypatch.setattr(estimate, "run_rclone", fake)
    outcome = await estimate.run_estimate(
        ProgressRecorder(), CONFIG, REMOTE, "their-bucket"
    )
    assert outcome.success is False
    assert outcome.failure_reason == "unparseable_output"
    # rclone exited 0 and the agent refused its output: no upstream code to
    # name, so the error derives from the reason alone (CORE-011 d9).
    assert outcome.exit_code == 0
    assert outcome.finer_error() is None


@pytest.mark.asyncio
async def test_timeout_is_a_named_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRclone({"size": TimeoutError()})
    monkeypatch.setattr(estimate, "run_rclone", fake)
    outcome = await estimate.run_estimate(
        ProgressRecorder(), CONFIG, REMOTE, "their-bucket"
    )
    assert outcome.success is False
    assert outcome.failure_reason == "timeout"
