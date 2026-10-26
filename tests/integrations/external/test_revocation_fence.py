"""Revocation fences future execution on a running agent (CORE-007 D3).

The audit's two bypasses: ``integration_load`` revoked before a cold start still
imported and ran; ``command_contributor`` revoked after boot still dispatched."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from stormpulse.agent import Agent, dispatch
from stormpulse.agent.dispatch import dispatch_message
from stormpulse.agent.external_adapters import (
    AdapterFenced,
    load_and_register_external,
)
from stormpulse.auth import NonceStore
from stormpulse.commands import CommandError, get_command
from stormpulse.commands.jobs import JobManager
from stormpulse.config import CommandSpec, Config, StorageConfig
from stormpulse.integrations.external import grants, loader
from stormpulse.integrations.external.model import CapabilityRequest as Cap
from stormpulse.protocol import Envelope
from stormpulse.signoff import SignoffState
from tests.helpers import (
    SECRET,
    build_config,
    matched_test_session,
    signed_command_request,
    signed_command_sequence,
)

from ._helpers import approve, keypair, state_dir
from .test_loader import (  # noqa: F401  (_isolate: autouse)
    _install_and_seal,
    _isolate,
    _registered,
)


@pytest.fixture
def nonces(tmp_path: Path) -> Iterator[NonceStore]:
    store = NonceStore(tmp_path / "nonces.db")
    yield store
    store.close()


def _sealed(tmp_path: Path, integration_id: str) -> tuple[Path, str]:
    private, fp = keypair()
    state = state_dir(tmp_path)
    approve(state, tmp_path, private)
    return state, _install_and_seal(
        tmp_path, state, private, fp, integration_id=integration_id
    )


def _agent(cfg: Config, nonces: NonceStore, signoff_dir: Path) -> Agent:
    return matched_test_session(
        Agent(
            cfg,
            SECRET,
            nonces,
            MagicMock(),
            asyncio.Event(),
            signoff_state=SignoffState(signoff_dir),
        )
    )


def _ws_payloads(ws: AsyncMock) -> list[dict[str, Any]]:
    return [json.loads(c.args[0])["payload"] for c in ws.send.call_args_list]


class _Live:
    """A booted agent over one sealed adapter, its results captured."""

    def __init__(
        self, tmp_path: Path, state: Path, integration_id: str, nonces: NonceStore
    ) -> None:
        cfg = replace(
            build_config(tmp_path, integrations={integration_id: {}}),
            storage=StorageConfig(db_path=state / "agent.db"),
        )
        self.agent = _agent(cfg, nonces, state)
        self.sent: list[Envelope] = []

        async def send(envelope: Envelope) -> None:
            self.sent.append(envelope)

        self.jobs = JobManager(cfg.agent.id, send)
        self.agent.job_manager = self.jobs
        self.ws = AsyncMock()

    def results(self) -> list[dict[str, Any]]:
        return _ws_payloads(self.ws) + [
            e.payload for e in self.sent if e.type.value == "command.result"
        ]

    async def dispatch(self, frame: Any) -> list[dict[str, Any]]:
        await dispatch_message(self.agent, self.ws, frame.to_json())
        await asyncio.gather(*list(self.jobs._jobs.values()))
        return self.results()


def test_load_revoked_before_cold_start_imports_nothing_on_either_entry_point(
    tmp_path: Path,
) -> None:
    state, digest = _sealed(tmp_path, "adcold")
    grants.revoke(state, package_digest=digest, capability=Cap.INTEGRATION_LOAD)

    assert loader.load_sealed_adapters(state) == []
    with pytest.raises(RuntimeError, match="integration_load is revoked"):
        loader.load_one_sealed_adapter(state, "adcold")
    assert "adcold.integration" not in sys.modules
    assert _registered("adcold") is None


@pytest.mark.asyncio
async def test_load_revoked_before_cold_start_leaves_no_command(
    tmp_path: Path, nonces: NonceStore
) -> None:
    state, digest = _sealed(tmp_path, "adcoldrun")
    grants.revoke(state, package_digest=digest, capability=Cap.INTEGRATION_LOAD)
    live = _Live(tmp_path, state, "adcoldrun", nonces)

    assert "adcoldrun_do" not in live.agent.registry
    (result,) = await live.dispatch(signed_command_request(command="adcoldrun_do"))
    assert result["success"] is False and result["stdout"] != "did it"
    assert "adcoldrun.integration" not in sys.modules


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", [Cap.COMMAND_CONTRIBUTOR, Cap.INTEGRATION_LOAD])
async def test_revoked_after_boot_refuses_the_advertised_command(
    tmp_path: Path, nonces: NonceStore, capability: Cap
) -> None:
    state, digest = _sealed(tmp_path, "adwarm")
    live = _Live(tmp_path, state, "adwarm", nonces)
    assert "adwarm_do" in live.agent.registry  # advertised at boot

    grants.revoke(state, package_digest=digest, capability=capability)
    (result,) = await live.dispatch(signed_command_request(command="adwarm_do"))
    assert result["success"] is False
    assert result["failure_reason"] == "forbidden"
    assert result["stdout"] != "did it"


@pytest.mark.asyncio
async def test_a_job_queued_before_the_revoke_never_reaches_the_adapter(
    tmp_path: Path, nonces: NonceStore
) -> None:
    state, digest = _sealed(tmp_path, "adqueued")
    live = _Live(tmp_path, state, "adqueued", nonces)
    jobs = live.jobs

    await jobs._sem.acquire()  # every permit taken: the job queues
    request = signed_command_request(command="adqueued_do")
    await dispatch_message(live.agent, live.ws, request.to_json())
    grants.revoke(state, package_digest=digest, capability=Cap.COMMAND_CONTRIBUTOR)
    jobs._sem.release()
    await asyncio.gather(*list(jobs._jobs.values()))

    (result,) = live.results()
    assert result["failure_reason"] == "forbidden"
    assert result["stdout"] != "did it"


def test_contributor_revoked_keeps_observation_load_revoked_fences_it(
    tmp_path: Path,
) -> None:
    state, digest = _sealed(tmp_path, "adobserve")
    load_and_register_external(state)
    integ = _registered("adobserve")
    assert integ is not None and integ.components is not None

    grants.revoke(state, package_digest=digest, capability=Cap.COMMAND_CONTRIBUTOR)
    assert integ.enabled({}) is True
    assert integ.components({}, None)

    grants.revoke(state, package_digest=digest, capability=Cap.INTEGRATION_LOAD)
    for call in (
        lambda: integ.enabled({}),
        lambda: integ.components({}, None),
        lambda: integ.parse_config({}),
    ):
        with pytest.raises(AdapterFenced, match="integration_load revoked"):
            call()


@pytest.mark.parametrize("damage", ["missing", "unreadable"])
def test_a_grant_that_cannot_be_read_holds_nothing(tmp_path: Path, damage: str) -> None:
    state, digest = _sealed(tmp_path, "adlost")
    load_and_register_external(state)
    integ = _registered("adlost")
    assert integ is not None and integ.specs is not None
    (spec,) = integ.specs({}).values()
    assert spec.authorize is not None and spec.authorize() is None

    path = grants.grant_path(state, "adlost", digest)
    if damage == "missing":
        path.unlink()
    else:
        path.write_text("{not json")
    assert grants.current_capabilities(state, "adlost", digest) == frozenset()
    assert spec.authorize() is not None
    with pytest.raises(AdapterFenced):
        integ.enabled({})


def _fenced_spec(**overrides: Any) -> CommandSpec:
    fields: dict[str, Any] = {
        "group": "adseq",
        "command": ["/bin/true"],
        "timeout": 5,
        "authorize": lambda: "command_contributor revoked",
    }
    return CommandSpec(**{**fields, **overrides})


def test_a_fenced_subprocess_command_is_refused_by_the_whitelist() -> None:
    with pytest.raises(CommandError, match="fenced") as caught:
        get_command("adseq_run", registry={"adseq_run": _fenced_spec()})
    assert caught.value.reason.value == "forbidden"


@pytest.mark.asyncio
async def test_a_sequence_step_whose_grant_is_revoked_never_runs(
    tmp_path: Path, nonces: NonceStore
) -> None:
    agent = _agent(build_config(tmp_path), nonces, tmp_path)
    agent.registry["adseq_run"] = _fenced_spec()
    ws = AsyncMock()

    await dispatch_message(agent, ws, signed_command_sequence(["adseq_run"]).to_json())
    (payload,) = _ws_payloads(ws)
    assert payload["failure_reason"] == "forbidden"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["job", "refresh"])
async def test_dispatch_refuses_a_fenced_command_before_any_mode_runs(
    tmp_path: Path, nonces: NonceStore, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dispatch gate is the only check a refresh meets, and it keeps a job's
    factory (adapter code) from running at all."""
    factory = MagicMock(return_value=None)
    refresh = AsyncMock()
    monkeypatch.setattr(dispatch, "handle_refresh", refresh)
    agent = _agent(build_config(tmp_path), nonces, tmp_path)
    agent.registry["adseq_x"] = _fenced_spec(
        command=["adseq_x"], mode=mode, handler=factory if mode == "job" else None
    )
    ws = AsyncMock()

    await dispatch_message(
        agent, ws, signed_command_request(command="adseq_x").to_json()
    )
    (payload,) = _ws_payloads(ws)
    assert payload["failure_reason"] == "forbidden"
    factory.assert_not_called()
    refresh.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_revoke_between_sequence_steps_refuses_the_next_step(
    tmp_path: Path, nonces: NonceStore
) -> None:
    # The pre-check passes; the step itself meets the revoke.
    answers = iter([None, "command_contributor revoked"])
    agent = _agent(build_config(tmp_path), nonces, tmp_path)
    agent.registry["adseq_ok"] = _fenced_spec(authorize=None)
    agent.registry["adseq_run"] = _fenced_spec(authorize=lambda: next(answers))
    ws = AsyncMock()

    frame = signed_command_sequence(["adseq_ok", "adseq_run"])
    await dispatch_message(agent, ws, frame.to_json())
    first, second = _ws_payloads(ws)
    assert first["success"] is True
    assert (second["step_index"], second["failure_reason"]) == (1, "forbidden")
