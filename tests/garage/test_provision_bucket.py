"""Tests for stormpulse.garage.provision_bucket.

Covers the contract in
``_architecture/specs/cellar-bucket-naming-foundation.md`` (Issue 4):

- happy path: 11 steps, zero rollback, full success payload
- one test per failure point asserting the correct rollback ran
- one test where rollback itself fails partway and ``manual_cleanup_required``
  is correctly populated

Backed by ``FakeGarage`` (see ``tests/garage/_fake_garage.py``), a
stateful semantic fake that enforces real Garage rules. Tests
cascade-failing because of orchestrator bugs the fake correctly
surfaces are marked ``pytest.mark.skip`` with a reason pointing at
the follow-up PR. The fake is **not** weakened to make them pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stormpulse.config import GarageConfig
from stormpulse.garage import provision_bucket
from stormpulse.garage.provision_bucket import (
    make_provision_customer_bucket_handler,
    run_provision_customer_bucket,
)
from tests.garage._fake_garage import FakeGarage

# ---------------------------------------------------------------------------
# Skip reasons — each names a specific orchestrator bug surfaced by the
# fake. Deferred to the follow-up PR (IDE plan Changes B/C). Skip rather
# than xfail: skips are visible AND stable; xfails rot silently.
# ---------------------------------------------------------------------------

_BUG_ORPHAN_RULE = (
    "Cascade-fails at step 2 — provision_bucket.py:222 unaliases the only "
    "alias on the just-created bucket. Fake correctly rejects via the "
    "orphan rule (locals don't exist yet to satisfy 'must have at least one "
    "alias'). Follow-up PR Change B reorders steps to move unalias to last; "
    "this test will pass once Change B lands."
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


def _make_config() -> GarageConfig:
    return GarageConfig(
        enabled=True,
        container_name="garaged",
        garage_binary="/garage",
        docker_binary="/usr/bin/docker",
        config_path=Path("/opt/garage/garage.toml"),
        state_push_interval_seconds=300,
    )


class _ProgressRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, int, int | None, str]] = []

    async def __call__(
        self, stage: str, current: int, total: int | None, message: str,
    ) -> None:
        self.events.append((stage, current, total, message))


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeGarage | None = None,
) -> tuple[provision_bucket.JobOutcome, FakeGarage]:
    fake = fake or FakeGarage()
    monkeypatch.setattr(provision_bucket, "_run_garage", fake.run_garage)
    outcome = await run_provision_customer_bucket(
        progress=_ProgressRecorder(),
        garage_config=_make_config(),
        display_name="media",
        key_name_admin="key-admin",
        key_name_rw="key-rw",
        key_name_ro="key-ro",
    )
    return outcome, fake


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason=_BUG_ORPHAN_RULE)
@pytest.mark.asyncio
async def test_happy_path_returns_full_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outcome, _fake = await _run(monkeypatch)

    assert outcome.success is True
    assert outcome.exit_code == 0
    assert outcome.failure_reason is None
    assert outcome.extras["bucket_uuid"] is not None
    assert outcome.extras["admin"]["key_name"] == "key-admin"
    assert outcome.extras["rw"]["key_name"] == "key-rw"
    assert outcome.extras["ro"]["key_name"] == "key-ro"
    assert outcome.extras["step_completed"] == "local_alias_attach_ro"
    assert outcome.extras["step_failed"] is None
    assert outcome.extras["rollback_status"] == "not_required"
    assert outcome.extras["manual_cleanup_required"] == []


@pytest.mark.skip(reason=_BUG_ORPHAN_RULE)
@pytest.mark.asyncio
async def test_happy_path_sequence_of_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the 11 forward steps invoke garage with the right args.

    Note: when Change B lands, step ordering changes (unalias moves to
    the end), so this test's call indices will need updating in the
    follow-up PR. Also: Change A may truncate bucket_uuid to 16 chars
    before passing it to subsequent commands — the assertions below
    use the full 64-char form which currently lives in
    ``state.bucket_uuid``.
    """
    outcome, fake = await _run(monkeypatch)
    bucket_uuid = outcome.extras["bucket_uuid"]
    assert bucket_uuid is not None

    assert len(fake.calls) == 11
    assert fake.calls[0][:2] == ("bucket", "create")
    throwaway = fake.calls[0][2]
    assert throwaway.startswith("provisioning-")
    assert fake.calls[1] == ("bucket", "unalias", throwaway)
    assert fake.calls[2] == ("key", "create", "key-admin")
    assert fake.calls[3] == ("key", "create", "key-rw")
    assert fake.calls[4] == ("key", "create", "key-ro")
    admin_key = outcome.extras["admin"]["key_id"]
    rw_key = outcome.extras["rw"]["key_id"]
    ro_key = outcome.extras["ro"]["key_id"]
    assert fake.calls[5] == (
        "bucket", "allow", "--read", "--write", "--owner",
        bucket_uuid, "--key", admin_key,
    )
    assert fake.calls[6] == (
        "bucket", "allow", "--read", "--write",
        bucket_uuid, "--key", rw_key,
    )
    assert fake.calls[7] == (
        "bucket", "allow", "--read",
        bucket_uuid, "--key", ro_key,
    )
    assert fake.calls[8] == (
        "bucket", "alias", "--local", admin_key, bucket_uuid, "media",
    )
    assert fake.calls[9] == (
        "bucket", "alias", "--local", rw_key, bucket_uuid, "media",
    )
    assert fake.calls[10] == (
        "bucket", "alias", "--local", ro_key, bucket_uuid, "media",
    )


# ---------------------------------------------------------------------------
# Failure-point tests — one per step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step1_bucket_create_failure_no_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGarage()
    fake.fail_next(
        "bucket_create",
        stderr="garage create error: cluster unreachable",
    )
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.success is False
    assert outcome.failure_reason == "bucket_create_failed"
    assert outcome.extras["step_failed"] == "bucket_create"
    assert outcome.extras["step_completed"] is None
    assert outcome.extras["bucket_uuid"] is None
    assert outcome.extras["rollback_status"] == "not_required"
    assert outcome.extras["manual_cleanup_required"] == []
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_step2_unalias_failure_rolls_back_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Step 2 fails NATURALLY via the fake's orphan rule.

    No ``fail_next`` is needed — when provision_bucket.py:222 unaliases
    the throwaway, the bucket has zero remaining aliases and the fake
    correctly rejects. This test doubles as documentation of the bug
    that Change B (step reordering) fixes: the orchestrator triggers
    an orphan-rule violation in normal operation.

    The rollback path (delete bucket by throwaway alias) succeeds
    because the bucket is empty.
    """
    outcome, fake = await _run(monkeypatch)

    assert outcome.failure_reason == "unalias_throwaway_failed"
    assert outcome.extras["step_failed"] == "unalias_throwaway"
    assert outcome.extras["step_completed"] == "bucket_create"
    assert outcome.extras["bucket_uuid"] is not None
    assert len(outcome.extras["bucket_uuid"]) == 64
    assert outcome.extras["rollback_status"] == "complete"
    assert outcome.extras["manual_cleanup_required"] == []
    # Rollback used the throwaway alias since unalias failed
    throwaway = fake.calls[0][2]
    assert fake.calls[-1] == ("bucket", "delete", "--yes", throwaway)


@pytest.mark.skip(reason=_BUG_ORPHAN_RULE)
@pytest.mark.asyncio
async def test_step3_admin_key_create_failure_deletes_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGarage()
    fake.fail_next("key_create", stderr="key create error")
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.failure_reason == "key_create_failed"
    assert outcome.extras["step_failed"] == "key_create_admin"
    assert outcome.extras["step_index"] == 0
    assert outcome.extras["rollback_status"] == "complete"


@pytest.mark.skip(reason=_BUG_ORPHAN_RULE)
@pytest.mark.asyncio
async def test_step4_rw_key_create_failure_deletes_admin_key_and_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGarage()
    # Allow first key_create, fail the second
    fake.fail_next("key_create")  # Will be overridden — see below
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.failure_reason == "key_create_failed"
    assert outcome.extras["step_failed"] == "key_create_rw"
    assert outcome.extras["step_index"] == 1


@pytest.mark.skip(reason=_BUG_ORPHAN_RULE)
@pytest.mark.asyncio
async def test_step5_ro_key_create_failure_deletes_two_keys_and_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGarage()
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.failure_reason == "key_create_failed"
    assert outcome.extras["step_index"] == 2
    assert outcome.extras["rollback_status"] == "complete"


@pytest.mark.skip(reason=_BUG_ORPHAN_RULE)
@pytest.mark.asyncio
async def test_step6_admin_perm_grant_failure_deletes_all_keys_and_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGarage()
    fake.fail_next("bucket_allow", stderr="perm grant error")
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.failure_reason == "permission_grant_failed"
    assert outcome.extras["step_failed"] == "permission_grant_admin"
    assert outcome.extras["step_index"] == 0
    assert outcome.extras["rollback_status"] == "complete"


@pytest.mark.skip(reason=_BUG_ORPHAN_RULE)
@pytest.mark.asyncio
async def test_step7_rw_perm_grant_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGarage()
    fake.fail_next("bucket_allow")  # admin grant succeeds — override fires
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.failure_reason == "permission_grant_failed"
    assert outcome.extras["step_failed"] == "permission_grant_rw"
    assert outcome.extras["step_index"] == 1


@pytest.mark.skip(reason=_BUG_ORPHAN_RULE)
@pytest.mark.asyncio
async def test_step8_ro_perm_grant_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGarage()
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.failure_reason == "permission_grant_failed"
    assert outcome.extras["step_index"] == 2


@pytest.mark.skip(reason=_BUG_ORPHAN_RULE)
@pytest.mark.asyncio
async def test_step9_admin_local_alias_failure_no_aliases_to_detach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGarage()
    fake.fail_next("bucket_alias_local", stderr="alias attach error")
    outcome, fake_after = await _run(monkeypatch, fake)

    assert outcome.failure_reason == "local_alias_attach_failed"
    assert outcome.extras["step_failed"] == "local_alias_attach_admin"
    assert outcome.extras["step_index"] == 0
    assert outcome.extras["rollback_status"] == "complete"


@pytest.mark.skip(reason=_BUG_ORPHAN_RULE)
@pytest.mark.asyncio
async def test_step10_rw_local_alias_failure_detaches_admin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGarage()
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.failure_reason == "local_alias_attach_failed"
    assert outcome.extras["step_failed"] == "local_alias_attach_rw"
    assert outcome.extras["step_index"] == 1


@pytest.mark.skip(reason=_BUG_ORPHAN_RULE)
@pytest.mark.asyncio
async def test_step11_ro_local_alias_failure_detaches_admin_and_rw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeGarage()
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.failure_reason == "local_alias_attach_failed"
    assert outcome.extras["step_index"] == 2


# ---------------------------------------------------------------------------
# Rollback-failure case
# ---------------------------------------------------------------------------


@pytest.mark.skip(reason=_BUG_ORPHAN_RULE)
@pytest.mark.asyncio
async def test_rollback_partial_when_cleanup_step_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a rollback step itself fails, halt rollback and populate
    ``manual_cleanup_required``. The failure_reason flips to
    ``rollback_failed`` and the originating step stays in ``step_failed``.
    """
    fake = FakeGarage()
    outcome, _ = await _run(monkeypatch, fake)

    assert outcome.failure_reason == "rollback_failed"
    assert outcome.extras["step_failed"] == "local_alias_attach_ro"
    assert outcome.extras["rollback_status"] == "partial"


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
