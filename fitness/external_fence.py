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

from stormpulse.agent.external_adapters import _translate
from stormpulse.integrations.external import grants, layout
from stormpulse.integrations.external.model import CapabilityRequest, SealedGrantV1
from stormpulse.sdk import (
    SdkCommandSpec,
    SdkIntegration,
    SdkJobOutcome,
    command_specs_digest,
)

_ID = "fencefitness"


def check_external_fence() -> list[str]:
    """Return violation strings; empty list means clean."""
    reached: list[str] = []

    def tripwire(name: str, result: Any) -> Any:
        def call(*_args: Any, **_kwargs: Any) -> Any:
            reached.append(name)
            return result

        return call

    async def job(_progress: Any) -> SdkJobOutcome:
        reached.append("job body")
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
        f.name: tripwire(f.name, None)
        for f in dataclasses.fields(SdkIntegration)
        if "Callable" in str(f.type)
    }
    hooks["specs"] = tripwire("specs", commands)
    sdk = SdkIntegration(id=_ID, **hooks)
    full = (CapabilityRequest.INTEGRATION_LOAD, CapabilityRequest.COMMAND_CONTRIBUTOR)
    grant = SealedGrantV1(
        agent_id="fitness",
        integration_id=_ID,
        publisher_fingerprint="sha256:" + "ab" * 32,
        package_digest="sha256:" + "cd" * 32,
        manifest_digest="sha256:" + "ef" * 32,
        granted_capabilities=full,
        sealed_at="2026-10-26T00:00:00Z",
        command_specs_digest=command_specs_digest(commands),
    )

    with tempfile.TemporaryDirectory(prefix="fitness-fence-") as tmp:
        state = Path(tmp)
        _persist(state, grant)
        integ = _translate(sdk, grant, state)
        assert integ.specs is not None
        specs = integ.specs({})
        violations = [
            f"command {n!r} carries no authorize"
            for n, s in specs.items()
            if s.authorize is None
        ]

        _persist(state, dataclasses.replace(grant, revoked_capabilities=(full[1],)))
        reached.clear()
        for name, spec in specs.items():
            if spec.authorize is not None and spec.authorize() is None:
                violations.append(
                    f"command {name!r} authorized after command_contributor revoked"
                )
            if spec.handler is not None:
                asyncio.run(spec.handler({})(_no_progress))  # the queued attempt
        violations += [f"{n} ran after command_contributor revoked" for n in reached]

        _persist(state, dataclasses.replace(grant, revoked_capabilities=(full[0],)))
        reached.clear()
        for field in dataclasses.fields(integ):
            hook = getattr(integ, field.name)
            if callable(hook) and not isinstance(hook, type):
                try:
                    hook(*[{}] * len(inspect.signature(hook).parameters))
                except Exception:  # noqa: BLE001, S110 - a refusal is the pass; reaching adapter code is the fail
                    pass
        violations += [
            f"adapter {n} ran after integration_load revoked" for n in reached
        ]
    return violations


def _persist(state: Path, grant: SealedGrantV1) -> None:
    path = grants.grant_path(state, grant.integration_id, grant.package_digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    layout.atomic_write(path, layout.canonical_json(grants.to_dict(grant)))


async def _no_progress(*_args: Any, **_kwargs: Any) -> None:
    return None
