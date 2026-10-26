"""Function 19: every external adapter seam answers to the current grant (CORE-007 D3).

Translates a probe adapter whose every ``Callable`` field is a tripwire, then
revokes on disk. With ``command_contributor`` revoked, every command must refuse
and no queued job may reach adapter code; with ``integration_load`` revoked, no
callback on the translated Integration may. A new seam passed through raw fails here.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import tempfile
from pathlib import Path
from typing import Any

from stormpulse.agent.external_adapters import AdapterFenced, _translate
from stormpulse.config import CommandSpec
from stormpulse.integrations import Integration
from stormpulse.integrations.external import grants, layout
from stormpulse.integrations.external.model import CapabilityRequest, SealedGrantV1
from stormpulse.sdk import (
    SdkCommandSpec,
    SdkIntegration,
    SdkJobOutcome,
    command_specs_digest,
)

_ID = "fencefitness"
_LOAD = CapabilityRequest.INTEGRATION_LOAD
_CONTRIBUTOR = CapabilityRequest.COMMAND_CONTRIBUTOR


class _Tripwires:
    """Records every adapter callable that runs; any record is a breach."""

    def __init__(self) -> None:
        self.reached: list[str] = []

    def wire(self, name: str, result: Any = None) -> Any:
        def call(*_args: Any, **_kwargs: Any) -> Any:
            self.reached.append(name)
            return result

        return call

    def drain(self, when: str) -> list[str]:
        breaches = [f"adapter {n} ran after {when}" for n in self.reached]
        self.reached.clear()
        return breaches


def check_external_fence() -> list[str]:
    """Return violation strings; empty list means clean."""
    trips = _Tripwires()
    sdk, grant = _probe(trips)
    with tempfile.TemporaryDirectory(prefix="fitness-fence-") as tmp:
        state = Path(tmp)
        _persist(state, grant)
        integ = _translate(sdk, grant, state)
        assert integ.specs is not None
        specs = integ.specs({})
        trips.reached.clear()

        _persist(
            state, dataclasses.replace(grant, revoked_capabilities=(_CONTRIBUTOR,))
        )
        violations = _commands_still_run(specs, trips)
        violations += trips.drain("command_contributor revoked")

        _persist(state, dataclasses.replace(grant, revoked_capabilities=(_LOAD,)))
        violations += _callbacks_not_refused(integ)
        violations += trips.drain("integration_load revoked")
    return violations


def _probe(trips: _Tripwires) -> tuple[SdkIntegration, SealedGrantV1]:
    """An adapter with one command per executing mode and every hook a tripwire."""

    async def job(_progress: Any) -> SdkJobOutcome:
        trips.reached.append("job body")
        return SdkJobOutcome(success=True)

    commands = {
        f"{_ID}_run": SdkCommandSpec(group=_ID, command=["/bin/true"], timeout=5),
        f"{_ID}_job": SdkCommandSpec(
            group=_ID,
            command=[f"{_ID}_job"],
            timeout=5,
            mode="job",
            handler=lambda _p: job,
        ),
    }
    hooks = {
        f.name: trips.wire(f.name)
        for f in dataclasses.fields(SdkIntegration)
        if "Callable" in str(f.type)
    }
    hooks["specs"] = trips.wire("specs", commands)
    grant = SealedGrantV1(
        agent_id="fitness",
        integration_id=_ID,
        publisher_fingerprint=_digest("ab"),
        package_digest=_digest("cd"),
        manifest_digest=_digest("ef"),
        granted_capabilities=(_LOAD, _CONTRIBUTOR),
        sealed_at="2026-10-26T00:00:00Z",
        command_specs_digest=command_specs_digest(commands),
    )
    return SdkIntegration(id=_ID, **hooks), grant


def _commands_still_run(specs: dict[str, CommandSpec], trips: _Tripwires) -> list[str]:
    violations: list[str] = []
    for name, spec in specs.items():
        if spec.authorize is None or spec.authorize() is None:
            violations.append(
                f"command {name!r} authorized after command_contributor revoked"
            )
        if spec.handler is not None:
            asyncio.run(spec.handler({})(trips.wire("progress")))  # the queued attempt
    return violations


def _callbacks_not_refused(integ: Integration) -> list[str]:
    """Calls every callable on the Integration; each must refuse with AdapterFenced."""
    violations: list[str] = []
    for field in dataclasses.fields(integ):
        hook = getattr(integ, field.name)
        if not callable(hook) or isinstance(hook, type):
            continue
        try:
            hook(*({} for _ in inspect.signature(hook).parameters))
        except AdapterFenced:
            continue
        violations.append(f"{field.name} did not refuse after integration_load revoked")
    return violations


def _digest(pair: str) -> str:
    return "sha256:" + pair * 32


def _persist(state: Path, grant: SealedGrantV1) -> None:
    path = grants.grant_path(state, grant.integration_id, grant.package_digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    layout.atomic_write(path, layout.canonical_json(grants.to_dict(grant)))
