"""Tests for stormpulse.garage.delete_provisioned_bucket.

The orchestrator deletes a provisioned bucket atomically, including
its aliases. The interesting case is post-A+B+C buckets that have
only local aliases - Garage v2.2.0's CLI deadlocks on these:

- ``bucket delete --yes <id>`` rejects with "still has other local
  aliases. Use ``bucket unalias`` to delete them one by one."
- ``bucket unalias --local <key> <name>`` rejects on the LAST alias
  with "doesn't have other aliases, please delete it instead of just
  unaliasing."

The orchestrator breaks the deadlock by attaching a temporary global
alias before detaching locals, then deletes via the temp global.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stormpulse.commands.jobs import JobOutcome
from stormpulse.config import GarageConfig
from stormpulse.garage import delete_provisioned_bucket, provision_bucket
from stormpulse.garage.delete_provisioned_bucket import (
    make_delete_provisioned_bucket_handler,
    run_delete_provisioned_bucket,
)
from tests.garage._fake_garage import FakeGarage


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


def _setup_post_provision_bucket(
    fake: FakeGarage, *, n_locals: int = 1,
) -> tuple[str, list[tuple[str, str]]]:
    """Create a bucket in the post-A+B+C shape: zero global aliases,
    N local aliases attached. Returns (bucket_id_16char, local_aliases)
    where local_aliases is a list of (key_id, alias_name) tuples.

    Each key gets read+write+owner permission AND a local alias -
    matching what the provision orchestrator does. The fake's
    ``bucket info`` rendering only surfaces keys that have permissions,
    so granting them is necessary for ``parse_bucket_info`` to see the
    locals.

    Uses the fake's internal helpers to skip orchestrator calls and
    avoid recording them in fake.calls - keeps the test's call list
    clean for assertions.
    """
    bucket = fake.add_bucket("temp-provisioning-alias")
    bucket_id = bucket.bucket_id
    local_aliases: list[tuple[str, str]] = []
    for i in range(n_locals):
        key = fake.add_key(f"key-{i}")
        # Grant permissions so the key shows up in bucket info.
        rc, _, stderr = fake._bucket_allow_or_deny(
            ("--read", "--write", "--owner",
             "temp-provisioning-alias", "--key", key.key_id),
            deny=False,
        )
        if rc != 0:
            raise ValueError(f"setup grant perms failed: {stderr}")
        # Attach local alias.
        alias_name = f"obsidian-{i}" if n_locals > 1 else "obsidian"
        rc, _, stderr = fake._bucket_alias_local(
            key.key_id, "temp-provisioning-alias", alias_name,
        )
        if rc != 0:
            raise ValueError(f"setup local alias failed: {stderr}")
        local_aliases.append((key.key_id, alias_name))
    # Detach the temp global so the bucket has ONLY locals.
    rc, _, stderr = fake._bucket_unalias_global("temp-provisioning-alias")
    if rc != 0:
        raise ValueError(f"setup detach global failed: {stderr}")
    # Reset fake.calls so test assertions only see orchestrator calls.
    fake.calls.clear()
    return bucket_id[:16], local_aliases


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeGarage,
    bucket_id: str,
) -> JobOutcome:
    monkeypatch.setattr(
        delete_provisioned_bucket, "run_garage", fake.run_garage,
    )
    # The orchestrator imports run_garage from provision_bucket; patch
    # both call sites so the fake intercepts everything.
    monkeypatch.setattr(provision_bucket, "run_garage", fake.run_garage)
    return await run_delete_provisioned_bucket(
        progress=_ProgressRecorder(),
        garage_config=_make_config(),
        bucket_id=bucket_id,
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_a_b_c_bucket_with_one_local_alias_deletes_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical case: bucket has zero globals + one local alias.
    Garage CLI normally deadlocks on this; orchestrator unblocks via
    a temp global alias.
    """
    fake = FakeGarage()
    bucket_id, _ = _setup_post_provision_bucket(fake, n_locals=1)

    outcome = await _run(monkeypatch, fake, bucket_id)

    assert outcome.success is True
    assert outcome.failure_reason is None
    assert outcome.extras["step_completed"] == "key_cleanup"
    # Bucket actually gone from the cluster.
    assert bucket_id[:16] not in {b.bucket_id[:16] for b in fake.buckets.values()}


@pytest.mark.asyncio
async def test_post_a_b_c_bucket_uses_temp_global_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the temp-global-alias trick is actually applied. The
    sequence should include a ``bucket alias <id> pulse-delete-XXX``
    early on, and the final delete uses that name.
    """
    fake = FakeGarage()
    bucket_id, _ = _setup_post_provision_bucket(fake, n_locals=1)

    await _run(monkeypatch, fake, bucket_id)

    alias_calls = [c for c in fake.calls if c[:2] == ("bucket", "alias")]
    assert len(alias_calls) == 1
    # Shape: ("bucket", "alias", <bucket_id>, <temp_global>)
    assert alias_calls[0][2] == bucket_id
    temp_global = alias_calls[0][3]
    assert temp_global.startswith("pulse-delete-")

    # Final delete should be addressed by the temp global, not by id.
    delete_calls = [c for c in fake.calls if c[:2] == ("bucket", "delete")]
    assert len(delete_calls) == 1
    assert delete_calls[0] == ("bucket", "delete", "--yes", temp_global)


@pytest.mark.asyncio
async def test_three_local_aliases_all_detached_before_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy 3-key buckets (admin/rw/ro) have 3 local aliases - all
    must be detached before the final delete.
    """
    fake = FakeGarage()
    bucket_id, locals_ = _setup_post_provision_bucket(fake, n_locals=3)

    outcome = await _run(monkeypatch, fake, bucket_id)

    assert outcome.success is True
    unalias_local_calls = [
        c for c in fake.calls if c[:3] == ("bucket", "unalias", "--local")
    ]
    assert len(unalias_local_calls) == 3
    assert bucket_id[:16] not in {b.bucket_id[:16] for b in fake.buckets.values()}


@pytest.mark.asyncio
async def test_bucket_with_global_alias_skips_temp_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the bucket already has a global alias (legacy / website-
    hosted), use it as the final delete reference instead of attaching
    a temporary one.
    """
    fake = FakeGarage()
    bucket = fake.add_bucket("alice-site")  # has global "alice-site"
    bucket_id = bucket.bucket_id
    # Attach a local alias too (mixed shape) - grant permissions so
    # the key surfaces in bucket info.
    key = fake.add_key("alice-site-all")
    fake._bucket_allow_or_deny(
        ("--read", "--write", "--owner",
         "alice-site", "--key", key.key_id),
        deny=False,
    )
    fake._bucket_alias_local(key.key_id, "alice-site", "site")
    fake.calls.clear()

    outcome = await _run(monkeypatch, fake, bucket_id[:16])

    assert outcome.success is True
    # Should NOT add a temp pulse-delete-XXX alias.
    alias_calls = [c for c in fake.calls if c[:2] == ("bucket", "alias")]
    assert alias_calls == []
    # Final delete uses the existing global alias.
    delete_calls = [c for c in fake.calls if c[:2] == ("bucket", "delete")]
    assert delete_calls[0] == ("bucket", "delete", "--yes", "alice-site")


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_already_absent_bucket_returns_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the bucket doesn't exist, the orchestrator returns success
    (idempotent) - the goal state is reached.
    """
    fake = FakeGarage()
    nonexistent_id = "0" * 16

    outcome = await _run(monkeypatch, fake, nonexistent_id)

    assert outcome.success is True
    assert outcome.extras.get("already_absent") is True
    # Should only have made a single bucket info call, no delete attempt.
    assert len(fake.calls) == 1
    assert fake.calls[0][:2] == ("bucket", "info")


# ---------------------------------------------------------------------------
# Failure cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bucket_not_empty_exits_at_step_1_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``bucket info`` reports objects, the orchestrator fails fast
    at step 1 without attaching the temp alias or detaching any locals.

    Cheaper (saves 2 + N Garage calls), simpler (no rollback runs), and
    safer (the only Garage state read is ``bucket info``, so nothing can
    leak if a later step's rollback itself fails). Late discovery -
    ``bucket info`` reports zero but ``bucket delete`` still trips
    ``BucketNotEmpty`` because objects landed in between - is covered by
    the step-4 BucketNotEmpty handling further down.
    """
    fake = FakeGarage()
    bucket_id, _ = _setup_post_provision_bucket(fake, n_locals=1)
    bucket = next(iter(fake.buckets.values()))
    object.__setattr__(bucket, "object_count", 5)

    outcome = await _run(monkeypatch, fake, bucket_id)

    assert outcome.success is False
    assert outcome.failure_reason == "bucket_not_empty"
    # Bucket and its 1 local alias survive untouched - no temp global
    # was ever attached, no locals were ever detached.
    assert bucket_id[:16] in {b.bucket_id[:16] for b in fake.buckets.values()}
    surviving = next(iter(fake.buckets.values()))
    assert len(surviving.local_aliases) == 1
    assert all(
        not g.startswith("pulse-delete-") for g in surviving.global_aliases
    )
    # Friendly stderr message names the count so the dashboard's toast
    # can surface it verbatim.
    assert "5 object" in outcome.stderr


@pytest.mark.asyncio
async def test_local_alias_detach_failure_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a local-alias detach fails midway, rollback re-attaches
    what was already detached and drops the temp global.
    """
    fake = FakeGarage()
    bucket_id, locals_ = _setup_post_provision_bucket(fake, n_locals=3)
    # Fail the SECOND local-alias detach. The first succeeds; rollback
    # then re-attaches that one and drops the temp global.
    fake.fail_next("bucket_unalias_local", after=1, stderr="injected fail")

    outcome = await _run(monkeypatch, fake, bucket_id)

    assert outcome.success is False
    assert outcome.failure_reason == "local_alias_detach_failed"
    # Bucket still alive.
    assert bucket_id[:16] in {b.bucket_id[:16] for b in fake.buckets.values()}
    surviving = next(iter(fake.buckets.values()))
    # All 3 locals back.
    assert len(surviving.local_aliases) == 3
    # No leftover temp global.
    assert all(
        not g.startswith("pulse-delete-") for g in surviving.global_aliases
    )


# ---------------------------------------------------------------------------
# Handler factory
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Step 5: key cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_step_5_deletes_unmoored_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After bucket delete, the orchestrator iterates the keys that
    had access to the bucket. Each key with zero remaining buckets
    is deleted (orphan credential cleanup).
    """
    fake = FakeGarage()
    bucket_id, locals_ = _setup_post_provision_bucket(fake, n_locals=1)
    key_ids_before = set(fake.keys.keys())
    assert len(key_ids_before) == 1

    outcome = await _run(monkeypatch, fake, bucket_id)

    assert outcome.success is True
    # Key gone from fake state.
    assert fake.keys == {}
    # Orchestrator surfaces what it deleted vs. what it skipped.
    assert outcome.extras["keys_deleted"] == list(key_ids_before)
    assert outcome.extras["keys_skipped"] == []
    # Step 5 actually issued key info + key delete for the key.
    key_info_calls = [c for c in fake.calls if c[:2] == ("key", "info")]
    key_delete_calls = [c for c in fake.calls if c[:2] == ("key", "delete")]
    assert len(key_info_calls) == 1
    assert len(key_delete_calls) == 1


@pytest.mark.asyncio
async def test_step_5_preserves_keys_with_other_buckets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key that's shared between buckets (e.g. an ops-attached key
    with permissions on multiple buckets) must NOT be deleted when one
    of its buckets goes away. ``key info`` reports remaining buckets
    and the orchestrator skips the delete.
    """
    fake = FakeGarage()
    # Bucket A - the one we're about to delete. Has a local alias on
    # the shared key.
    bucket_a = fake.add_bucket("temp-provisioning-a")
    bucket_a_id = bucket_a.bucket_id
    shared_key = fake.add_key("shared-ops-key")
    fake._bucket_allow_or_deny(
        ("--read", "--write", "--owner",
         "temp-provisioning-a", "--key", shared_key.key_id),
        deny=False,
    )
    fake._bucket_alias_local(
        shared_key.key_id, "temp-provisioning-a", "alias-on-a",
    )
    fake._bucket_unalias_global("temp-provisioning-a")

    # Bucket B - survives. Same key has access here too.
    bucket_b = fake.add_bucket("bucket-b")
    fake._bucket_allow_or_deny(
        ("--read", "--write", "--owner",
         "bucket-b", "--key", shared_key.key_id),
        deny=False,
    )
    fake.calls.clear()

    outcome = await _run(monkeypatch, fake, bucket_a_id[:16])

    assert outcome.success is True
    # Bucket A gone, bucket B preserved.
    assert bucket_a_id[:16] not in {b.bucket_id[:16] for b in fake.buckets.values()}
    assert bucket_b.bucket_id in fake.buckets
    # Shared key preserved - has remaining bucket.
    assert shared_key.key_id in fake.keys
    assert outcome.extras["keys_deleted"] == []
    assert outcome.extras["keys_skipped"] == [shared_key.key_id]


def test_handler_factory_returns_none_on_missing_bucket_id() -> None:
    handler = make_delete_provisioned_bucket_handler(
        _make_config(), params={},
    )
    assert handler is None


def test_handler_factory_returns_handler_when_complete() -> None:
    handler = make_delete_provisioned_bucket_handler(
        _make_config(), params={"bucket_id": "abc1234567890def"},
    )
    assert handler is not None
    assert callable(handler)
