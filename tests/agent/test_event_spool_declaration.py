"""An integration's declared event source (CORE-011 d6): declared in the
artifact outside the digest, and refused at boot with only its own spool
when the SDK or the directory checks say no."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from stormpulse import events
from stormpulse.agent.event_spool import SpoolFeeder, spool_feeders
from stormpulse.agent.external_adapters import _event_source
from stormpulse.agent.wire_contract import build_wire_contract, contract_digest
from stormpulse.sdk import SdkEventSource
from stormpulse.sdk.declaration import _EVENT_ENVELOPE
from tests.agent.spool_support import NOBODY, SOURCE, declared, runtime

pytestmark = pytest.mark.usefixtures("event_registry")


def test_the_artifact_gains_the_declared_source_outside_the_digest(
    feeder: SpoolFeeder,
) -> None:
    artifact = build_wire_contract()
    assert artifact["integration_events"]["sources"] == {
        SOURCE: {"request_seen": ["duration_ms", "record_id", "status"]}
    }
    assert SOURCE not in artifact["messages"]["events.batch"]["sources"]
    events._INTEGRATION_REGISTRY.clear()
    assert contract_digest() == artifact["contract_digest"]


def test_a_source_name_already_declared_is_refused(spool: Path) -> None:
    taken = _event_source(
        SdkEventSource(source="jobs", kinds={"x": ()}, spool_dir=str(spool))
    )
    assert spool_feeders({"taken": runtime("live", taken)}) == {}
    assert "jobs" not in events.declared_integration_events()


def test_a_spool_already_fed_is_refused_for_the_second_source(
    feeder: SpoolFeeder, spool: Path
) -> None:
    alias = spool.parent / "alias"
    alias.symlink_to(spool.parent)
    other = declared(alias / spool.name, name="other_gate")
    assert spool_feeders({"other": runtime("live", other)}) == {}
    assert list(events.declared_integration_events()) == [SOURCE]


@pytest.mark.parametrize(
    ("arrange", "problem"),
    [
        (lambda s: s.chmod(0o755), "open to others"),
        pytest.param(
            lambda s: s.chmod(0o500),
            "not writable",
            marks=pytest.mark.skipif(
                os.geteuid() == 0, reason="root bypasses directory write permission"
            ),
        ),
        (lambda s: s.rmdir(), "No such file"),
        (lambda s: (s.rmdir(), s.symlink_to(s.parent)), "symlink"),
    ],
    ids=["open", "read-only", "missing", "symlink"],
)
def test_a_spool_the_agent_should_not_feed_from_refuses_its_source(
    spool: Path, arrange: Any, problem: str, caplog: pytest.LogCaptureFixture
) -> None:
    arrange(spool)
    source = declared(spool)
    assert spool_feeders({"probe": runtime("live", source)}) == {}
    assert problem in caplog.text
    assert SOURCE not in events.declared_integration_events()


@pytest.mark.parametrize(
    ("kinds", "spool", "problem"),
    [
        ({"seen": ("seq",)}, "/spool", "envelope fields"),
        ({"seen": ("ok",)}, "spool", "absolute"),
        ({"Seen": ("ok",)}, "/spool", "names must match"),
        ({}, "/spool", "no kinds"),
        ({"seen": ("ok",)}, "/a/../spool", "normalised"),
        ({"seen": ("ok",)}, "/spool/", "normalised"),
        ({"seen": ("ok",)}, "/", "root directory"),
    ],
)
def test_the_sdk_refuses_a_bad_declaration(
    kinds: dict[str, tuple[str, ...]], spool: str, problem: str
) -> None:
    with pytest.raises(ValueError, match=problem):
        SdkEventSource(source="gate", kinds=kinds, spool_dir=spool)


def test_the_sdk_envelope_mirrors_the_agent_envelope() -> None:
    """The SDK imports nothing from the agent, so its copy is pinned here."""
    assert _EVENT_ENVELOPE == events.ENVELOPE_FIELDS


def test_only_a_live_integration_with_a_source_gets_a_feeder_and_a_declaration(
    tmp_path: Path,
) -> None:
    spools = [tmp_path / n for n in ("fed", "off")]
    for path in spools:
        path.mkdir(mode=0o700)
    fed, off = (declared(p, name=f"{p.name}_gate") for p in spools)
    feeders = spool_feeders(
        {
            "fed": runtime("live", fed),
            "off": runtime("disabled", off),
            "mute": runtime("live", None),
        }
    )
    assert list(feeders) == ["fed"]
    assert feeders["fed"].source.name == "fed_gate"
    assert list(events.declared_integration_events()) == ["fed_gate"]


def test_the_translator_carries_the_declared_source_onto_the_integration(
    spool: Path,
) -> None:
    """Without this hop no adapter's source reaches ``spool_feeders``."""
    from stormpulse.agent.external_adapters import _translate
    from stormpulse.integrations.external.model import SealedGrantV1
    from stormpulse.sdk import SdkIntegration

    sdk = SdkIntegration(
        id="probe",
        parse_config=lambda c: c,
        enabled=lambda c: True,
        event_source=SdkEventSource(
            source=SOURCE, kinds={"seen": ("record_id",)}, spool_dir=str(spool)
        ),
    )
    grant = SealedGrantV1(
        agent_id="a",
        integration_id=sdk.id,
        publisher_fingerprint="sha256:" + "ab" * 32,
        package_digest="sha256:" + "cd" * 32,
        manifest_digest="sha256:" + "ef" * 32,
        granted_capabilities=(),
        sealed_at="2026-10-13T00:00:00Z",
    )
    assert _translate(sdk, grant).event_source == events.EventSource(
        name=SOURCE, kinds={"seen": frozenset({"record_id"})}, spool_dir=spool
    )


def test_a_refused_source_costs_only_itself(tmp_path: Path) -> None:
    bad, good = tmp_path / "bad", tmp_path / "good"
    bad.mkdir(mode=0o755)
    bad.chmod(0o755)
    good.mkdir(mode=0o700)
    feeders = spool_feeders(
        {
            "bad": runtime("live", declared(bad, name="bad_gate")),
            "good": runtime("live", declared(good, name="good_gate")),
        }
    )
    assert list(feeders) == ["good"]


@pytest.mark.skipif(
    os.geteuid() != 0, reason="needs root to create a file another user owns"
)
def test_a_spool_owned_by_another_user_refuses_its_source(
    spool: Path, caplog: pytest.LogCaptureFixture
) -> None:
    os.chown(spool, NOBODY, NOBODY)
    assert spool_feeders({"probe": runtime("live", declared(spool))}) == {}
    assert f"owned by uid {NOBODY}" in caplog.text


def test_a_spool_that_is_a_file_refuses_its_source(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "spool"
    path.write_text("")
    path.chmod(0o700)  # passes every other check, so only the type refuses it
    assert spool_feeders({"probe": runtime("live", declared(path))}) == {}
    assert "not a directory" in caplog.text
