"""Tests for stormpulse.garage.provision_bucket.

Covers the contract in
``_architecture/specs/cellar-bucket-naming-foundation.md`` (Issue 4):

- happy path: 11 steps, zero rollback, full success payload
- one test per failure point asserting the correct rollback ran
- one test where rollback itself fails partway and ``manual_cleanup_required``
  is correctly populated
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from stormpulse.config import GarageConfig
from stormpulse.garage import provision_bucket
from stormpulse.garage.provision_bucket import (
    make_provision_customer_bucket_handler,
    run_provision_customer_bucket,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


_BUCKET_UUID = (
    "ee224218a98dd4cd8b08b3386cd6791a24ca456ba6ab19bbc90fdc574c291a75"
)


def _bucket_info_stdout(global_alias: str) -> str:
    return (
        "==== BUCKET INFORMATION ====\n"
        f"Bucket:          {_BUCKET_UUID}\n"
        "Created:         2026-05-06 12:00:00.000 +00:00\n"
        "\n"
        "Size:            0 B (0 B)\n"
        "Objects:         0\n"
        "\n"
        "Website access:  false\n"
        "\n"
        f"Global alias:    {global_alias}\n"
        "\n"
        "==== KEYS FOR THIS BUCKET ====\n"
        "Permissions  Access key    Local aliases\n"
    )


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
    """Scripted replacement for ``_run_garage``.

    Pop responses off ``script`` in order. Each entry is either a
    ``(rc, stdout, stderr)`` tuple OR an Exception to raise.
    """

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


def _ok_bucket_create(throwaway_capture: list[str]) -> tuple[int, str, str]:
    """Bucket-create response. Throwaway captured later from runner.calls."""
    # We don't know the throwaway alias at script-construction time
    # because it's randomly generated. The bucket info parses bucket_id
    # regardless of what alias is in it, so we use a placeholder.
    return (0, _bucket_info_stdout("placeholder"), "")


def _scripted_happy_path() -> list[tuple[int, str, str] | Exception]:
    """Eleven OK responses for the full forward flow."""
    return [
        # Step 1: bucket create
        (0, _bucket_info_stdout("placeholder"), ""),
        # Step 2: bucket unalias
        (0, "", ""),
        # Step 3-5: key creates
        (0, _key_create_stdout("GK_ADMIN", "key-admin", "SECRET_ADMIN"), ""),
        (0, _key_create_stdout("GK_RW", "key-rw", "SECRET_RW"), ""),
        (0, _key_create_stdout("GK_RO", "key-ro", "SECRET_RO"), ""),
        # Step 6-8: permission grants
        (0, "", ""),
        (0, "", ""),
        (0, "", ""),
        # Step 9-11: local alias attaches
        (0, "", ""),
        (0, "", ""),
        (0, "", ""),
    ]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_full_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _FakeRunner(_scripted_happy_path())
    monkeypatch.setattr(provision_bucket, "_run_garage", runner)
    progress = _ProgressRecorder()

    outcome = await run_provision_customer_bucket(
        progress=progress,
        garage_config=_make_config(),
        display_name="media",
        key_name_admin="usr-1-media-all",
        key_name_rw="usr-1-media-rw",
        key_name_ro="usr-1-media-ro",
    )

    assert outcome.success is True
    assert outcome.exit_code == 0
    assert outcome.failure_reason is None
    assert outcome.extras["bucket_uuid"] == _BUCKET_UUID
    assert outcome.extras["admin"] == {
        "key_id": "GK_ADMIN",
        "secret": "SECRET_ADMIN",
        "key_name": "usr-1-media-all",
    }
    assert outcome.extras["rw"]["key_id"] == "GK_RW"
    assert outcome.extras["ro"]["key_id"] == "GK_RO"
    assert outcome.extras["step_completed"] == "local_alias_attach_ro"
    assert outcome.extras["step_failed"] is None
    assert outcome.extras["rollback_status"] == "not_required"
    assert outcome.extras["manual_cleanup_required"] == []
    assert runner.idx == 11


@pytest.mark.asyncio
async def test_happy_path_sequence_of_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify the 11 forward steps invoke garage with the right args."""
    runner = _FakeRunner(_scripted_happy_path())
    monkeypatch.setattr(provision_bucket, "_run_garage", runner)
    progress = _ProgressRecorder()

    await run_provision_customer_bucket(
        progress=progress,
        garage_config=_make_config(),
        display_name="media",
        key_name_admin="key-admin",
        key_name_rw="key-rw",
        key_name_ro="key-ro",
    )

    assert len(runner.calls) == 11
    # Step 1: bucket create <throwaway>
    assert runner.calls[0][:2] == ("bucket", "create")
    throwaway = runner.calls[0][2]
    assert throwaway.startswith("_provisioning_")
    # Step 2: bucket unalias <throwaway>
    assert runner.calls[1] == ("bucket", "unalias", throwaway)
    # Step 3-5: key create
    assert runner.calls[2] == ("key", "create", "key-admin")
    assert runner.calls[3] == ("key", "create", "key-rw")
    assert runner.calls[4] == ("key", "create", "key-ro")
    # Step 6: admin perms (--read --write --owner)
    assert runner.calls[5] == (
        "bucket", "allow", "--read", "--write", "--owner",
        _BUCKET_UUID, "--key", "GK_ADMIN",
    )
    # Step 7: rw perms (--read --write)
    assert runner.calls[6] == (
        "bucket", "allow", "--read", "--write",
        _BUCKET_UUID, "--key", "GK_RW",
    )
    # Step 8: ro perms (--read)
    assert runner.calls[7] == (
        "bucket", "allow", "--read",
        _BUCKET_UUID, "--key", "GK_RO",
    )
    # Step 9-11: local alias attaches by UUID + display_name
    assert runner.calls[8] == (
        "bucket", "alias", "--local", "GK_ADMIN", _BUCKET_UUID, "media",
    )
    assert runner.calls[9] == (
        "bucket", "alias", "--local", "GK_RW", _BUCKET_UUID, "media",
    )
    assert runner.calls[10] == (
        "bucket", "alias", "--local", "GK_RO", _BUCKET_UUID, "media",
    )


# ---------------------------------------------------------------------------
# Failure-point tests — one per step
# ---------------------------------------------------------------------------


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    script: list[tuple[int, str, str] | Exception],
) -> tuple[provision_bucket.JobOutcome, _FakeRunner]:
    runner = _FakeRunner(script)
    monkeypatch.setattr(provision_bucket, "_run_garage", runner)
    outcome = await run_provision_customer_bucket(
        progress=_ProgressRecorder(),
        garage_config=_make_config(),
        display_name="media",
        key_name_admin="key-admin",
        key_name_rw="key-rw",
        key_name_ro="key-ro",
    )
    return outcome, runner


@pytest.mark.asyncio
async def test_step1_bucket_create_failure_no_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, runner = await _run(monkeypatch, [
        (1, "", "garage create error: cluster unreachable"),
    ])

    assert outcome.success is False
    assert outcome.failure_reason == "bucket_create_failed"
    assert outcome.extras["step_failed"] == "bucket_create"
    assert outcome.extras["step_completed"] is None
    assert outcome.extras["bucket_uuid"] is None
    assert outcome.extras["rollback_status"] == "not_required"
    assert outcome.extras["manual_cleanup_required"] == []
    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_step2_unalias_failure_rolls_back_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, runner = await _run(monkeypatch, [
        (0, _bucket_info_stdout("placeholder"), ""),  # step 1 OK
        (1, "", "unalias error"),                      # step 2 fail
        (0, "", ""),                                   # rollback: bucket delete OK
    ])

    assert outcome.failure_reason == "unalias_throwaway_failed"
    assert outcome.extras["step_failed"] == "unalias_throwaway"
    assert outcome.extras["step_completed"] == "bucket_create"
    assert outcome.extras["bucket_uuid"] == _BUCKET_UUID
    assert outcome.extras["rollback_status"] == "complete"
    assert outcome.extras["manual_cleanup_required"] == []
    # Rollback used the throwaway alias since unalias failed
    throwaway = runner.calls[0][2]
    assert runner.calls[-1] == ("bucket", "delete", "--yes", throwaway)


@pytest.mark.asyncio
async def test_step3_admin_key_create_failure_deletes_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, runner = await _run(monkeypatch, [
        (0, _bucket_info_stdout("placeholder"), ""),  # step 1
        (0, "", ""),                                   # step 2
        (1, "", "key create error"),                   # step 3 fail
        (0, "", ""),                                   # rollback: bucket delete by UUID
    ])

    assert outcome.failure_reason == "key_create_failed"
    assert outcome.extras["step_failed"] == "key_create_admin"
    assert outcome.extras["step_index"] == 0
    assert outcome.extras["rollback_status"] == "complete"
    # After step 2 succeeded, throwaway is gone — delete by UUID
    assert runner.calls[-1] == ("bucket", "delete", "--yes", _BUCKET_UUID)


@pytest.mark.asyncio
async def test_step4_rw_key_create_failure_deletes_admin_key_and_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, runner = await _run(monkeypatch, [
        (0, _bucket_info_stdout("placeholder"), ""),  # 1
        (0, "", ""),                                   # 2
        (0, _key_create_stdout("GK_ADMIN", "k", "S"), ""),  # 3
        (1, "", "key create error"),                   # 4 fail
        (0, "", ""),                                   # rollback: delete admin key
        (0, "", ""),                                   # rollback: delete bucket
    ])

    assert outcome.failure_reason == "key_create_failed"
    assert outcome.extras["step_failed"] == "key_create_rw"
    assert outcome.extras["step_index"] == 1
    assert outcome.extras["rollback_status"] == "complete"
    # Last two calls are the rollback
    assert runner.calls[-2] == ("key", "delete", "--yes", "GK_ADMIN")
    assert runner.calls[-1] == ("bucket", "delete", "--yes", _BUCKET_UUID)


@pytest.mark.asyncio
async def test_step5_ro_key_create_failure_deletes_two_keys_and_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, runner = await _run(monkeypatch, [
        (0, _bucket_info_stdout("placeholder"), ""),  # 1
        (0, "", ""),                                   # 2
        (0, _key_create_stdout("GK_ADMIN", "k", "S"), ""),  # 3
        (0, _key_create_stdout("GK_RW", "k", "S"), ""),     # 4
        (1, "", "key create error"),                   # 5 fail
        (0, "", ""),                                   # rollback: delete admin
        (0, "", ""),                                   # rollback: delete rw
        (0, "", ""),                                   # rollback: delete bucket
    ])

    assert outcome.failure_reason == "key_create_failed"
    assert outcome.extras["step_index"] == 2
    assert outcome.extras["rollback_status"] == "complete"
    assert runner.calls[-3] == ("key", "delete", "--yes", "GK_ADMIN")
    assert runner.calls[-2] == ("key", "delete", "--yes", "GK_RW")
    assert runner.calls[-1] == ("bucket", "delete", "--yes", _BUCKET_UUID)


@pytest.mark.asyncio
async def test_step6_admin_perm_grant_failure_deletes_all_keys_and_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, runner = await _run(monkeypatch, [
        (0, _bucket_info_stdout("p"), ""),              # 1
        (0, "", ""),                                    # 2
        (0, _key_create_stdout("GK_ADMIN", "k", "S"), ""),  # 3
        (0, _key_create_stdout("GK_RW", "k", "S"), ""),     # 4
        (0, _key_create_stdout("GK_RO", "k", "S"), ""),     # 5
        (1, "", "perm grant error"),                    # 6 fail
        (0, "", ""), (0, "", ""), (0, "", ""),          # rollback: 3 key deletes
        (0, "", ""),                                    # rollback: bucket delete
    ])

    assert outcome.failure_reason == "permission_grant_failed"
    assert outcome.extras["step_failed"] == "permission_grant_admin"
    assert outcome.extras["step_index"] == 0
    assert outcome.extras["rollback_status"] == "complete"
    rollback_calls = runner.calls[-4:]
    assert rollback_calls[0] == ("key", "delete", "--yes", "GK_ADMIN")
    assert rollback_calls[1] == ("key", "delete", "--yes", "GK_RW")
    assert rollback_calls[2] == ("key", "delete", "--yes", "GK_RO")
    assert rollback_calls[3] == ("bucket", "delete", "--yes", _BUCKET_UUID)


@pytest.mark.asyncio
async def test_step7_rw_perm_grant_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome, runner = await _run(monkeypatch, [
        (0, _bucket_info_stdout("p"), ""),                  # 1
        (0, "", ""),                                        # 2
        (0, _key_create_stdout("GK_ADMIN", "k", "S"), ""),  # 3
        (0, _key_create_stdout("GK_RW", "k", "S"), ""),     # 4
        (0, _key_create_stdout("GK_RO", "k", "S"), ""),     # 5
        (0, "", ""),                                        # 6
        (1, "", "perm grant error"),                        # 7 fail
        (0, "", ""), (0, "", ""), (0, "", ""),              # rollback keys
        (0, "", ""),                                        # rollback bucket
    ])

    assert outcome.failure_reason == "permission_grant_failed"
    assert outcome.extras["step_failed"] == "permission_grant_rw"
    assert outcome.extras["step_index"] == 1
    assert outcome.extras["rollback_status"] == "complete"


@pytest.mark.asyncio
async def test_step8_ro_perm_grant_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    outcome, _ = await _run(monkeypatch, [
        (0, _bucket_info_stdout("p"), ""),                  # 1
        (0, "", ""),                                        # 2
        (0, _key_create_stdout("GK_ADMIN", "k", "S"), ""),  # 3
        (0, _key_create_stdout("GK_RW", "k", "S"), ""),     # 4
        (0, _key_create_stdout("GK_RO", "k", "S"), ""),     # 5
        (0, "", ""),                                        # 6
        (0, "", ""),                                        # 7
        (1, "", "perm grant error"),                        # 8 fail
        (0, "", ""), (0, "", ""), (0, "", ""),              # rollback keys
        (0, "", ""),                                        # rollback bucket
    ])

    assert outcome.failure_reason == "permission_grant_failed"
    assert outcome.extras["step_index"] == 2


@pytest.mark.asyncio
async def test_step9_admin_local_alias_failure_no_aliases_to_detach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, runner = await _run(monkeypatch, [
        (0, _bucket_info_stdout("p"), ""),                  # 1
        (0, "", ""),                                        # 2
        (0, _key_create_stdout("GK_ADMIN", "k", "S"), ""),  # 3
        (0, _key_create_stdout("GK_RW", "k", "S"), ""),     # 4
        (0, _key_create_stdout("GK_RO", "k", "S"), ""),     # 5
        (0, "", ""), (0, "", ""), (0, "", ""),              # 6-8 perms
        (1, "", "alias attach error"),                      # 9 fail
        (0, "", ""), (0, "", ""), (0, "", ""),              # rollback keys
        (0, "", ""),                                        # rollback bucket
    ])

    assert outcome.failure_reason == "local_alias_attach_failed"
    assert outcome.extras["step_failed"] == "local_alias_attach_admin"
    assert outcome.extras["step_index"] == 0
    assert outcome.extras["rollback_status"] == "complete"
    # No alias detach call (none were attached yet); first rollback
    # call is a key delete.
    rollback_calls = runner.calls[9:]  # everything after the failed step 9
    assert rollback_calls[0] == ("key", "delete", "--yes", "GK_ADMIN")


@pytest.mark.asyncio
async def test_step10_rw_local_alias_failure_detaches_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, runner = await _run(monkeypatch, [
        (0, _bucket_info_stdout("p"), ""),                  # 1
        (0, "", ""),                                        # 2
        (0, _key_create_stdout("GK_ADMIN", "k", "S"), ""),  # 3
        (0, _key_create_stdout("GK_RW", "k", "S"), ""),     # 4
        (0, _key_create_stdout("GK_RO", "k", "S"), ""),     # 5
        (0, "", ""), (0, "", ""), (0, "", ""),              # 6-8 perms
        (0, "", ""),                                        # 9 admin alias OK
        (1, "", "alias attach error"),                      # 10 fail
        (0, "", ""),                                        # rollback: detach admin alias
        (0, "", ""), (0, "", ""), (0, "", ""),              # rollback: 3 key deletes
        (0, "", ""),                                        # rollback: bucket delete
    ])

    assert outcome.failure_reason == "local_alias_attach_failed"
    assert outcome.extras["step_failed"] == "local_alias_attach_rw"
    assert outcome.extras["step_index"] == 1
    assert outcome.extras["rollback_status"] == "complete"
    rollback_calls = runner.calls[10:]
    # First rollback step: detach admin local alias
    assert rollback_calls[0] == (
        "bucket", "unalias", "--local", "GK_ADMIN", _BUCKET_UUID, "media",
    )


@pytest.mark.asyncio
async def test_step11_ro_local_alias_failure_detaches_admin_and_rw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, runner = await _run(monkeypatch, [
        (0, _bucket_info_stdout("p"), ""),                  # 1
        (0, "", ""),                                        # 2
        (0, _key_create_stdout("GK_ADMIN", "k", "S"), ""),  # 3
        (0, _key_create_stdout("GK_RW", "k", "S"), ""),     # 4
        (0, _key_create_stdout("GK_RO", "k", "S"), ""),     # 5
        (0, "", ""), (0, "", ""), (0, "", ""),              # 6-8
        (0, "", ""),                                        # 9 admin alias
        (0, "", ""),                                        # 10 rw alias
        (1, "", "alias attach error"),                      # 11 fail
        (0, "", ""), (0, "", ""),                           # rollback: detach 2 aliases
        (0, "", ""), (0, "", ""), (0, "", ""),              # rollback: 3 keys
        (0, "", ""),                                        # rollback: bucket
    ])

    assert outcome.failure_reason == "local_alias_attach_failed"
    assert outcome.extras["step_index"] == 2
    assert outcome.extras["rollback_status"] == "complete"
    rollback_calls = runner.calls[11:]
    assert rollback_calls[0] == (
        "bucket", "unalias", "--local", "GK_ADMIN", _BUCKET_UUID, "media",
    )
    assert rollback_calls[1] == (
        "bucket", "unalias", "--local", "GK_RW", _BUCKET_UUID, "media",
    )


# ---------------------------------------------------------------------------
# Rollback-failure case
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_partial_when_cleanup_step_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a rollback step itself fails, halt rollback and populate
    ``manual_cleanup_required``. The failure_reason flips to
    ``rollback_failed`` and the originating step stays in ``step_failed``.
    """
    outcome, runner = await _run(monkeypatch, [
        (0, _bucket_info_stdout("p"), ""),                  # 1
        (0, "", ""),                                        # 2
        (0, _key_create_stdout("GK_ADMIN", "k", "S"), ""),  # 3
        (0, _key_create_stdout("GK_RW", "k", "S"), ""),     # 4
        (0, _key_create_stdout("GK_RO", "k", "S"), ""),     # 5
        (0, "", ""), (0, "", ""), (0, "", ""),              # 6-8
        (0, "", ""),                                        # 9 admin alias
        (0, "", ""),                                        # 10 rw alias
        (1, "", "alias attach error"),                      # 11 fail
        # Rollback: first step (detach admin alias) fails
        (1, "", "unalias error during rollback"),
    ])

    assert outcome.failure_reason == "rollback_failed"
    # Originating step still recorded
    assert outcome.extras["step_failed"] == "local_alias_attach_ro"
    assert outcome.extras["rollback_status"] == "partial"
    cleanup = outcome.extras["manual_cleanup_required"]
    # Both attached aliases are still alive; all 3 keys are alive; the bucket is alive.
    types_ids = {(item["type"], item.get("key_id") or item.get("id"))
                 for item in cleanup}
    assert ("local_alias", "GK_ADMIN") in types_ids
    assert ("local_alias", "GK_RW") in types_ids
    assert ("key", "GK_ADMIN") in types_ids
    assert ("key", "GK_RW") in types_ids
    assert ("key", "GK_RO") in types_ids
    assert ("bucket", _BUCKET_UUID) in types_ids


# ---------------------------------------------------------------------------
# Handler factory
# ---------------------------------------------------------------------------


def test_handler_factory_returns_none_on_missing_params() -> None:
    handler = make_provision_customer_bucket_handler(
        _make_config(),
        params={"display_name": "media"},  # missing key names
    )
    assert handler is None


def test_handler_factory_returns_handler_when_complete() -> None:
    handler = make_provision_customer_bucket_handler(
        _make_config(),
        params={
            "display_name": "media",
            "key_name_admin": "k-a",
            "key_name_rw": "k-rw",
            "key_name_ro": "k-ro",
        },
    )
    assert handler is not None
    assert callable(handler)
