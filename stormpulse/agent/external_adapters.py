"""Entry-layer glue for sealed external adapters (CORE-007).

The loader (``integrations/external/loader.py``) executes and returns the loaded
``SdkIntegration`` objects; this module translates each into the internal
``registry.Integration`` and registers it. It lives in the Entry layer because
translating a command handler constructs a ``commands.jobs.JobOutcome``, which
the CORE-000 topology forbids ``integrations/`` from importing.

Two authorization gates live here:

- **id collision (D6):** an external id that matches a built-in is quarantined
  (built-ins always win) - the registry is idempotent-by-id and would otherwise
  silently drop it, so the loud, named refusal happens here.
- **command gate (D2):** the translated ``specs`` builder is a closure that, when
  the command table is assembled, returns commands only if ``command_contributor``
  is effective AND the recomputed ``command_specs_digest`` matches the seal;
  otherwise it returns nothing (the adapter still loads for state/health).

Command-name collision is enforced by the bootstrap command-table assembly,
where all command names meet.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from stormpulse.commands.jobs import JobOutcome, ProgressCallback
from stormpulse.config import CommandSpec, ParamDef
from stormpulse.integrations import Integration, register_integration, registered_integrations
from stormpulse.integrations.external import grants, loader
from stormpulse.integrations.external.model import CapabilityRequest, SealedGrantV1
from stormpulse.sdk import (
    SdkCommandHandler,
    SdkCommandSpec,
    SdkIntegration,
    SdkParamDef,
    command_specs_digest,
)

logger = logging.getLogger(__name__)


def load_and_register_external(state_dir: Path) -> frozenset[str]:
    """Load, translate, and register sealed external adapters. Returns the ids
    that registered (id-collision quarantines, built-ins winning)."""
    builtin_ids = {integ.id for integ in registered_integrations()}
    registered: set[str] = set()
    for adapter in loader.load_sealed_adapters(state_dir):
        sdk = adapter.integration
        if sdk.id in builtin_ids:
            logger.warning(
                "external adapter %r collides with a built-in id; quarantined (built-ins win)",
                sdk.id,
            )
            continue
        try:
            register_integration(_translate(sdk, adapter.grant))
            registered.add(sdk.id)
        except Exception as exc:  # noqa: BLE001 - one bad adapter must never crash the agent
            logger.warning(
                "external adapter %r failed to register (soft-disabled): %s", sdk.id, exc
            )
    return frozenset(registered)


def _translate(sdk: SdkIntegration, grant: SealedGrantV1) -> Integration:
    return Integration(
        id=sdk.id,
        parse_config=sdk.parse_config,
        enabled=sdk.enabled,
        preconditions=sdk.preconditions,
        specs=_gated_specs_builder(sdk, grant) if sdk.specs is not None else None,
        capabilities=sdk.capabilities,
        readiness=sdk.readiness,
    )


def _gated_specs_builder(sdk: SdkIntegration, grant: SealedGrantV1) -> Any:
    sdk_specs_of = sdk.specs
    assert sdk_specs_of is not None  # guarded by the caller

    def build(parsed: Any) -> dict[str, CommandSpec]:
        sdk_specs = dict(sdk_specs_of(parsed))
        if CapabilityRequest.COMMAND_CONTRIBUTOR not in grants.effective_capabilities(grant):
            logger.warning(
                "external adapter %r: command_contributor not granted; commands fenced", sdk.id
            )
            return {}
        if command_specs_digest(sdk_specs) != grant.command_specs_digest:
            logger.warning(
                "external adapter %r: command_specs_digest mismatch; commands fenced", sdk.id
            )
            return {}
        return {name: _translate_command_spec(spec) for name, spec in sdk_specs.items()}

    return build


def _translate_command_spec(spec: SdkCommandSpec) -> CommandSpec:
    return CommandSpec(
        group=spec.group,
        command=list(spec.command),
        timeout=spec.timeout,
        mode=spec.mode,
        requires_confirmation=spec.requires_confirmation,
        description=spec.description,
        sensitive_output=spec.sensitive_output,
        read_only=spec.read_only,
        self_reconciling=spec.self_reconciling,
        handler=_translate_handler(spec.handler) if spec.handler is not None else None,
        params={name: _translate_param(p) for name, p in spec.params.items()},
    )


def _translate_param(param: SdkParamDef) -> ParamDef:
    return ParamDef(
        placeholder=param.placeholder,
        default=param.default,
        pattern=param.pattern,
        description=param.description,
        max_bytes=param.max_bytes,
        secret=param.secret,
    )


def _translate_handler(sdk_handler: SdkCommandHandler) -> Any:
    """Wrap the SDK command handler (a factory returning an async job body over
    the SDK progress/outcome types) into the internal ``CommandHandler`` shape."""

    def factory(params: dict[str, str]) -> Any:
        sdk_job = sdk_handler(params)
        if sdk_job is None:
            return None

        async def run(progress: ProgressCallback) -> JobOutcome:
            outcome = await sdk_job(_ProgressAdapter(progress))
            return JobOutcome(
                success=outcome.success,
                exit_code=outcome.exit_code,
                stdout=outcome.stdout,
                stderr=outcome.stderr,
                failure_reason=outcome.failure_reason,
                extras=dict(outcome.extras),
            )

        return run

    return factory


class _ProgressAdapter:
    """Presents the internal ``ProgressCallback`` as the SDK's ``SdkProgress``."""

    def __init__(self, inner: ProgressCallback) -> None:
        self._inner = inner

    async def __call__(
        self,
        stage: str,
        current: int,
        total: int | None,
        message: str,
        *,
        bytes_freed: int | None = None,
    ) -> None:
        await self._inner(stage, current, total, message, bytes_freed=bytes_freed)
