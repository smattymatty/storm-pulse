"""The state walk against a real Garage: what the control plane is told.

``collect_garage_state`` is what every quota decision, usage report, and
reconcile pass upstream is built on. The fakes prove the composition; this
proves the composition survives contact with Garage's actual responses.

If this file goes red after a Garage upgrade, the control plane would have
been fed a wrong or empty picture of the node, silently.
"""

from __future__ import annotations

import time

from stormpulse.garage import admin_api
from stormpulse.garage.state import GarageState, GarageStateReader, collect_garage_state
from tests.wire.conftest import (
    WireBucket,
    WireEnv,
    garage_cli,
    pretty,
    put_object,
    unique_alias,
)


def _collect(wire: WireEnv) -> GarageState:
    state = collect_garage_state(wire.garage_config())
    assert state is not None, (
        "collect_garage_state returned None against a live Garage; the walk "
        "treats an unreachable admin API and an empty cluster the same way"
    )
    return state


def _await_bucket(wire: WireEnv, bucket: WireBucket, *, objects: int) -> GarageState:
    """Collect until the async-propagated counter shows the seeded objects."""
    last: GarageState | None = None
    for _ in range(40):
        state = _collect(wire)
        last = state
        found = next((b for b in state.buckets if b.alias == bucket.name), None)
        if found is not None and found.object_count == objects:
            return state
        time.sleep(0.25)
    assert last is not None
    raise AssertionError(
        f"bucket {bucket!r} never reported {objects} objects; last walk saw:\n"
        f"{pretty([b.alias for b in last.buckets])}"
    )


# ---------------------------------------------------------------------------
# Topology
# ---------------------------------------------------------------------------


def test_walk_reports_the_node_topology(wire: WireEnv) -> None:
    """node_id, version, and capacity come back populated and healthy.

    A version string is how the fleet answers "what is this node running".
    An empty one here means the dashboard shows nothing for every node.
    """
    state = _collect(wire)
    assert len(state.node_id) == 64, (
        f"node_id {state.node_id!r} is not Garage's full node id"
    )
    assert state.version.startswith("v"), f"version {state.version!r}"
    assert state.healthy is True
    assert state.capacity_gb > 0, "capacity did not survive the walk"


# ---------------------------------------------------------------------------
# Per-bucket accounting
# ---------------------------------------------------------------------------


def test_walk_reports_exact_usage_for_a_seeded_bucket(
    wire: WireEnv, bucket: WireBucket
) -> None:
    """size_bytes and object_count match exactly what was uploaded.

    The number the capacity model divides by. Off-by-a-unit here misprices
    every account on the node.
    """
    total = 0
    for i in range(3):
        body = b"s" * (500 + i)
        put_object(wire, bucket.name, f"usage-{i}.bin", body)
        total += len(body)

    state = _await_bucket(wire, bucket, objects=3)
    found = next(b for b in state.buckets if b.alias == bucket.name)
    assert found.object_count == 3
    assert found.size_bytes == total, f"walk says {found.size_bytes}, seeded {total}"
    # The agent reports Garage's full id and the control plane stores its
    # 16-char prefix. Truncating agent-side would make the state push
    # unable to address its own buckets on a mutating endpoint.
    assert len(found.id) == 64, f"walk must carry the full bucket id, got {found.id!r}"
    assert found.id == bucket.id


def test_walk_carries_the_quota_the_agent_set(wire: WireEnv, bucket: WireBucket) -> None:
    """A quota set through the admin API comes back on the walk.

    This is the loop closing: the agent caps a bucket, then reads its own cap
    back on the next state push. A break here makes the control plane believe
    every bucket is uncapped.
    """
    info, err = admin_api.get_bucket_info(**wire.admin_kwargs, bucket_ref=bucket.id)
    assert err == "", err
    assert info is not None

    want = 5_368_709_120  # 5 GiB
    ok, err = admin_api.set_bucket_quota(
        **wire.admin_kwargs, bucket_id=info["id"], max_size_bytes=want
    )
    assert ok, err

    state = _collect(wire)
    found = next((b for b in state.buckets if b.alias == bucket.name), None)
    assert found is not None, [b.alias for b in state.buckets]
    assert found.quota_max_size_bytes == want, (
        f"walk reported quota {found.quota_max_size_bytes}, set {want}"
    )


def test_walk_includes_a_bucket_with_no_alias(wire: WireEnv) -> None:
    """An alias-less bucket still appears in the walk.

    A nameless-but-owned bucket must be adopted, capped, and flagged, never
    skipped: skipping is not fail-safe for a capped resource. If the walk
    drops it, the account gets free uncapped storage and nothing alarms.
    """
    created, err = admin_api.create_bucket(**wire.admin_kwargs)
    assert err == "", err
    assert created is not None
    full_id = created["id"]
    try:
        state = _collect(wire)
        found = next((b for b in state.buckets if full_id.startswith(b.id)), None)
        assert found is not None, (
            "an alias-less bucket vanished from the walk; it would go uncapped\n"
            f"{pretty([(b.id, b.alias) for b in state.buckets])}"
        )
    finally:
        admin_api.delete_bucket(**wire.admin_kwargs, bucket_ref=full_id)


def test_walk_reports_key_grants_on_a_bucket(wire: WireEnv, bucket: WireBucket) -> None:
    """The harness key shows up as a grant on the bucket it owns.

    Bucket-to-key attribution is how usage is billed to an account.
    """
    state = _collect(wire)
    found = next(b for b in state.buckets if b.alias == bucket.name)
    assert any(k.key_id == wire.access_key for k in found.keys), pretty(
        [k.key_id for k in found.keys]
    )


# ---------------------------------------------------------------------------
# The periodic reader
# ---------------------------------------------------------------------------


def test_reader_collect_returns_a_state_and_force_topology_refreshes(
    wire: WireEnv,
) -> None:
    """The cadence-aware reader works against the real endpoint both ways.

    The periodic path (topology cached on a slow multiple) and the on-demand
    refresh path (``force_topology``) are different code; the on-demand one
    exists because serving cached topology made a refresh lie.
    """
    reader = GarageStateReader()
    first = reader.collect(wire.garage_config())
    assert first is not None

    forced = reader.collect(wire.garage_config(), force_topology=True)
    assert forced is not None
    assert forced.node_id == first.node_id


def test_walk_sees_a_bucket_created_after_the_first_collect(
    wire: WireEnv,
) -> None:
    """A new bucket appears on the next walk, never behind a stale cache.

    The detector's whole job: an S3-born bucket must be seen promptly so its
    uncapped window stays bounded.
    """
    reader = GarageStateReader()
    before = reader.collect(wire.garage_config())
    assert before is not None

    alias = unique_alias("late")
    created, err = admin_api.create_bucket(**wire.admin_kwargs, global_alias=alias)
    assert err == "", err
    assert created is not None
    try:
        after = reader.collect(wire.garage_config())
        assert after is not None
        assert any(b.alias == alias for b in after.buckets), (
            f"bucket created between collects was invisible to the second walk:\n"
            f"{pretty([b.alias for b in after.buckets])}"
        )
    finally:
        garage_cli("bucket", "delete", "--yes", alias)


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_walk_returns_none_when_the_admin_api_is_unreachable(
    wire: WireEnv,
) -> None:
    """An unreachable admin API yields None, never a half-empty state.

    Reporting a degraded snapshot as truth would tell the control plane the
    node has zero buckets, and zero buckets means zero usage.
    """
    import dataclasses

    broken = dataclasses.replace(
        wire.garage_config(), admin_url="http://127.0.0.1:1"
    )
    assert collect_garage_state(broken) is None


def test_walk_returns_none_when_the_admin_token_is_wrong(wire: WireEnv) -> None:
    """A rejected token is also None, not an empty-but-valid-looking node."""
    import dataclasses

    broken = dataclasses.replace(wire.garage_config(), admin_token="wrong-token")
    assert collect_garage_state(broken) is None
