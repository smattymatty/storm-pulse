"""Tests for stormpulse.garage.rotate_key.

Covers the four-step orchestrated key rotation:

- happy path: 4 steps, full success payload with new secret
- one test per failure point asserting the correct rollback ran
- tests for permission flags per tier (all / rw / ro)
- one test where rollback itself fails partway and ``manual_cleanup_required``
  is correctly populated
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stormpulse.config import GarageConfig
from stormpulse.garage import rotate_key
from stormpulse.garage.rotate_key import (
    make_rotate_customer_key_handler,
    run_rotate_customer_key,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_BUCKET_UUID = (
    "ee224218a98dd4cd8b08b3386cd6791a24ca456ba6ab19bbc90fdc574c291a75"
)
_OLD_KEY = "GK_OLD_42"
_NEW_KEY = "GK_NEW_99"


def _key_create_stdout(key_id: str, key_name: str, secret: str) -> str:
    return (
        f"Key name: {key_name}\n"
        f"Key ID: {key_id}\n"
        f"Secret key: {secret}\n"
    )


def _make_config() -> GarageConfig:
    return GarageConfig(
        enabled=True,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=Path("/opt/garage/garage.toml"),
        state_push_interval_seconds=300,
    )


class _FakeRunner:
    def __init__(
        self, script: list[tuple[int, str, str] | Exception],
    ) -> None:
        self.script = script
        self.calls: list[tuple[str, ...]] = []
        self.idx = 0

    async def __call__(
        self, garage_config: GarageConfig, *args: str, timeout: float = 30,
    ) -> tuple[int, str, str]:
        self.calls.append(args)
        if self.idx >= len(self.script):
            raise AssertionError(
                f"FakeRunner ran out of scripted responses at call {self.idx}: "
                f"args={args}",
            )
        response = self.script[self.idx]
        self.idx += 1
        if isinstance(response, Exception):
            raise response
        return response


class _ProgressRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, int, int | None, str]] = []

    async def __call__(
        self, stage: str, current: int, total: int | None, message: str,
    ) -> None:
        self.events.append((stage, current, total, message))


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    script: list[tuple[int, str, str] | Exception],
    *,
    key_tier: str = "all",
) -> tuple[rotate_key.JobOutcome, _FakeRunner]:
    runner = _FakeRunner(script)
    # rotate_key imports _run_garage by name from provision_bucket, so the
    # bound reference is in rotate_key's namespace — patch there.
    monkeypatch.setattr(rotate_key, "_run_garage", runner)
    outcome = await run_rotate_customer_key(
        progress=_ProgressRecorder(),
        garage_config=_make_config(),
        old_key_id=_OLD_KEY,
        new_key_name="usr-1-media-rw",
        bucket_id=_BUCKET_UUID,
        local_alias="media",
        key_tier=key_tier,
    )
    return outcome, runner


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_new_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, runner = await _run(monkeypatch, [
        # Step 1: key create
        (0, _key_create_stdout(_NEW_KEY, "usr-1-media-rw", "NEWSECRET"), ""),
        # Step 2: permission grant
        (0, "", ""),
        # Step 3: alias attach
        (0, "", ""),
        # Step 4: old key delete
        (0, "", ""),
    ])

    assert outcome.success is True
    assert outcome.failure_reason is None
    assert outcome.extras["new_key_id"] == _NEW_KEY
    assert outcome.extras["new_secret"] == "NEWSECRET"
    assert outcome.extras["new_key_name"] == "usr-1-media-rw"
    assert outcome.extras["step_completed"] == "old_key_delete"
    assert outcome.extras["step_failed"] is None
    assert outcome.extras["rollback_status"] == "not_required"
    assert outcome.extras["manual_cleanup_required"] == []

    assert len(runner.calls) == 4
    assert runner.calls[0] == ("key", "create", "usr-1-media-rw")
    assert runner.calls[1] == (
        "bucket", "allow", "--read", "--write", "--owner",
        _BUCKET_UUID, "--key", _NEW_KEY,
    )
    assert runner.calls[2] == (
        "bucket", "alias", "--local", _NEW_KEY, _BUCKET_UUID, "media",
    )
    assert runner.calls[3] == ("key", "delete", "--yes", _OLD_KEY)


@pytest.mark.asyncio
async def test_happy_path_tier_rw_uses_read_write_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, runner = await _run(monkeypatch, [
        (0, _key_create_stdout(_NEW_KEY, "n", "S"), ""),
        (0, "", ""),
        (0, "", ""),
        (0, "", ""),
    ], key_tier="rw")

    assert outcome.success is True
    assert runner.calls[1] == (
        "bucket", "allow", "--read", "--write",
        _BUCKET_UUID, "--key", _NEW_KEY,
    )
    assert "--owner" not in runner.calls[1]


@pytest.mark.asyncio
async def test_happy_path_tier_ro_uses_read_only_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, runner = await _run(monkeypatch, [
        (0, _key_create_stdout(_NEW_KEY, "n", "S"), ""),
        (0, "", ""),
        (0, "", ""),
        (0, "", ""),
    ], key_tier="ro")

    assert outcome.success is True
    assert runner.calls[1] == (
        "bucket", "allow", "--read",
        _BUCKET_UUID, "--key", _NEW_KEY,
    )
    assert "--write" not in runner.calls[1]
    assert "--owner" not in runner.calls[1]


# ---------------------------------------------------------------------------
# Failure points
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step1_new_key_create_failure_no_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, runner = await _run(monkeypatch, [
        (1, "", "key create error"),
    ])

    assert outcome.success is False
    assert outcome.failure_reason == "new_key_create_failed"
    assert outcome.extras["step_failed"] == "new_key_create"
    assert outcome.extras["new_key_id"] is None
    assert outcome.extras["rollback_status"] == "not_required"
    assert outcome.extras["manual_cleanup_required"] == []
    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_step2_permission_grant_failure_deletes_new_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, runner = await _run(monkeypatch, [
        # Step 1 OK
        (0, _key_create_stdout(_NEW_KEY, "n", "S"), ""),
        # Step 2 fail (permission grant)
        (1, "", "permission grant error"),
        # Rollback: delete new key
        (0, "", ""),
    ])

    assert outcome.failure_reason == "new_key_permission_grant_failed"
    assert outcome.extras["step_failed"] == "new_key_permission_grant"
    assert outcome.extras["new_key_id"] == _NEW_KEY
    assert outcome.extras["rollback_status"] == "complete"
    # Rollback should be exactly one call: delete the new key. No
    # alias detach (alias never attached), no permission revoke
    # (permissions never granted).
    assert len(runner.calls) == 3
    rollback_calls = runner.calls[2:]
    assert rollback_calls == [("key", "delete", "--yes", _NEW_KEY)]
    # Old key never touched
    delete_calls = [c for c in runner.calls if c[:2] == ("key", "delete")]
    assert ("key", "delete", "--yes", _OLD_KEY) not in delete_calls


@pytest.mark.asyncio
async def test_step3_alias_attach_failure_revokes_perms_and_deletes_new_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, runner = await _run(monkeypatch, [
        # Step 1 OK
        (0, _key_create_stdout(_NEW_KEY, "n", "S"), ""),
        # Step 2 OK (perm grant)
        (0, "", ""),
        # Step 3 fail (alias attach)
        (1, "", "alias attach error"),
        # Rollback: revoke permissions
        (0, "", ""),
        # Rollback: delete new key
        (0, "", ""),
    ])

    assert outcome.failure_reason == "new_key_alias_attach_failed"
    assert outcome.extras["step_failed"] == "new_key_alias_attach"
    assert outcome.extras["new_key_id"] == _NEW_KEY
    assert outcome.extras["rollback_status"] == "complete"
    # Rollback calls: bucket deny then key delete (no alias to detach)
    rollback_calls = runner.calls[3:]
    assert rollback_calls[0] == (
        "bucket", "deny", "--read", "--write", "--owner",
        _BUCKET_UUID, "--key", _NEW_KEY,
    )
    assert rollback_calls[1] == ("key", "delete", "--yes", _NEW_KEY)
    # Old key never touched
    delete_calls = [c for c in runner.calls if c[:2] == ("key", "delete")]
    assert ("key", "delete", "--yes", _OLD_KEY) not in delete_calls


@pytest.mark.asyncio
async def test_step4_old_key_delete_failure_full_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 4 (old key delete) is the last forward step. Full rollback."""
    outcome, runner = await _run(monkeypatch, [
        (0, _key_create_stdout(_NEW_KEY, "n", "S"), ""),  # 1
        (0, "", ""),                                       # 2 perm grant
        (0, "", ""),                                       # 3 alias attach
        (1, "", "old key delete error"),                   # 4 fail
        # Rollback: detach alias
        (0, "", ""),
        # Rollback: revoke permissions
        (0, "", ""),
        # Rollback: delete new key
        (0, "", ""),
    ])

    assert outcome.failure_reason == "old_key_delete_failed"
    assert outcome.extras["step_failed"] == "old_key_delete"
    assert outcome.extras["rollback_status"] == "complete"
    # Rollback calls in order: unalias, deny, delete new key.
    # Old key untouched.
    rollback_calls = runner.calls[4:]
    assert len(rollback_calls) == 3
    assert rollback_calls[0] == (
        "bucket", "unalias", "--local", _NEW_KEY, _BUCKET_UUID, "media",
    )
    assert rollback_calls[1] == (
        "bucket", "deny", "--read", "--write", "--owner",
        _BUCKET_UUID, "--key", _NEW_KEY,
    )
    assert rollback_calls[2] == ("key", "delete", "--yes", _NEW_KEY)
    # Old key never appears in rollback
    for call in rollback_calls:
        assert _OLD_KEY not in call


# ---------------------------------------------------------------------------
# Rollback-failure cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_partial_when_unalias_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, _runner = await _run(monkeypatch, [
        (0, _key_create_stdout(_NEW_KEY, "n", "S"), ""),  # 1
        (0, "", ""),                                       # 2 perm grant
        (0, "", ""),                                       # 3 alias attach
        (1, "", "old delete error"),                       # 4 fail
        # Rollback: unalias fails
        (1, "", "unalias error during rollback"),
    ])

    assert outcome.failure_reason == "rollback_failed"
    assert outcome.extras["step_failed"] == "old_key_delete"
    assert outcome.extras["rollback_status"] == "partial"
    cleanup = outcome.extras["manual_cleanup_required"]
    types_ids = {(item["type"], item.get("key_id") or item.get("id"))
                 for item in cleanup}
    # Alias still attached, permissions still granted, key still alive
    assert ("local_alias", _NEW_KEY) in types_ids
    assert ("permission_grant", _NEW_KEY) in types_ids
    assert ("key", _NEW_KEY) in types_ids


@pytest.mark.asyncio
async def test_rollback_partial_when_perm_revoke_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 4 fails, alias detach succeeds, permission revoke fails."""
    outcome, _runner = await _run(monkeypatch, [
        (0, _key_create_stdout(_NEW_KEY, "n", "S"), ""),  # 1
        (0, "", ""),                                       # 2 perm grant
        (0, "", ""),                                       # 3 alias attach
        (1, "", "old delete error"),                       # 4 fail
        # Rollback: unalias OK
        (0, "", ""),
        # Rollback: bucket deny fails
        (1, "", "deny error during rollback"),
    ])

    assert outcome.failure_reason == "rollback_failed"
    assert outcome.extras["rollback_status"] == "partial"
    cleanup = outcome.extras["manual_cleanup_required"]
    types_ids = {(item["type"], item.get("key_id") or item.get("id"))
                 for item in cleanup}
    # Alias was successfully detached, so it's not in cleanup. Perms
    # still granted, key still alive.
    assert ("local_alias", _NEW_KEY) not in types_ids
    assert ("permission_grant", _NEW_KEY) in types_ids
    assert ("key", _NEW_KEY) in types_ids


@pytest.mark.asyncio
async def test_rollback_partial_when_new_key_delete_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 2 fails (perm grant), and rollback's new-key delete also fails."""
    outcome, _ = await _run(monkeypatch, [
        (0, _key_create_stdout(_NEW_KEY, "n", "S"), ""),  # 1
        (1, "", "perm grant error"),                       # 2 fail
        # Rollback: delete new key fails
        (1, "", "key delete error during rollback"),
    ])

    assert outcome.failure_reason == "rollback_failed"
    assert outcome.extras["step_failed"] == "new_key_permission_grant"
    assert outcome.extras["rollback_status"] == "partial"
    cleanup = outcome.extras["manual_cleanup_required"]
    types_ids = {(item["type"], item.get("key_id") or item.get("id"))
                 for item in cleanup}
    # The new key wasn't deleted
    assert ("key", _NEW_KEY) in types_ids


# ---------------------------------------------------------------------------
# Handler factory
# ---------------------------------------------------------------------------


def test_handler_factory_returns_none_on_missing_params() -> None:
    handler = make_rotate_customer_key_handler(
        _make_config(),
        params={"old_key_id": _OLD_KEY},  # missing rest
    )
    assert handler is None


def test_handler_factory_returns_none_when_key_tier_missing() -> None:
    handler = make_rotate_customer_key_handler(
        _make_config(),
        params={
            "old_key_id": _OLD_KEY,
            "new_key_name": "new-key",
            "bucket_id": _BUCKET_UUID,
            "local_alias": "media",
            # key_tier missing
        },
    )
    assert handler is None


def test_handler_factory_returns_none_on_invalid_key_tier() -> None:
    handler = make_rotate_customer_key_handler(
        _make_config(),
        params={
            "old_key_id": _OLD_KEY,
            "new_key_name": "new-key",
            "bucket_id": _BUCKET_UUID,
            "local_alias": "media",
            "key_tier": "admin",  # not one of all/rw/ro
        },
    )
    assert handler is None


def test_handler_factory_returns_handler_when_complete() -> None:
    handler = make_rotate_customer_key_handler(
        _make_config(),
        params={
            "old_key_id": _OLD_KEY,
            "new_key_name": "new-key",
            "bucket_id": _BUCKET_UUID,
            "local_alias": "media",
            "key_tier": "all",
        },
    )
    assert handler is not None
    assert callable(handler)
