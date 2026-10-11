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

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.config import CommandSpec, ConfigError, ParamDef
from stormpulse.integrations import (
    Integration,
    register_integration,
    registered_integrations,
)
from stormpulse.integrations.external import grants, loader
from stormpulse.integrations.external.model import CapabilityRequest, SealedGrantV1
from stormpulse.protocol import FailureReason
from stormpulse.sdk import (
    SdkCommandHandler,
    SdkCommandSpec,
    SdkConfigError,
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
                "external adapter %r failed to register (soft-disabled): %s",
                sdk.id,
                exc,
            )
    return frozenset(registered)


def _wrap_parse_config(sdk_parse: Any) -> Any:
    """Map an adapter's config failure to the host's ConfigError, so bootstrap
    soft-disables it (CORE-005 d5 / D6). An adapter can't import ConfigError
    (SDK-only), so it raises SdkConfigError; any other parse failure is treated
    the same rather than crashing the agent."""

    def parse(raw: dict[str, object]) -> Any:
        try:
            return sdk_parse(raw)
        except SdkConfigError as exc:
            raise ConfigError(str(exc)) from exc
        except Exception as exc:
            raise ConfigError(
                "An external integration couldn't read its configuration "
                f"({type(exc).__name__}). Run 'stormpulse integration doctor', "
                "correct its configuration or update the integration, then restart Storm Pulse."
            ) from None

    return parse


def _translate(sdk: SdkIntegration, grant: SealedGrantV1) -> Integration:
    build, gate = (
        _gated_specs_builder(sdk, grant) if sdk.specs is not None else (None, None)
    )
    return Integration(
        id=sdk.id,
        in_release_catalog=False,
        parse_config=_wrap_parse_config(sdk.parse_config),
        enabled=sdk.enabled,
        preconditions=sdk.preconditions,
        specs=build,
        command_gate=gate,
        capabilities=sdk.capabilities,
        readiness=sdk.readiness,
        # Ungated, unlike specs: a deploy subject is data no allow rule binds
        # to, so it needs no command-contributor grant and moves no digest
        # (CORE-005 D14). The node's own table still overrides whatever it says.
        deploy_subjects=sdk.deploy_subjects,
        components=(
            _translate_components(sdk.components)
            if sdk.components is not None
            else None
        ),
    )


def _translate_components(sdk_components: Any) -> Any:
    """An external adapter holds no state, so its hook sees the config alone."""

    def components(config: Any, _state: Any) -> tuple[Any, ...]:
        return tuple(sdk_components(config))

    return components


def _gated_specs_builder(sdk: SdkIntegration, grant: SealedGrantV1) -> tuple[Any, Any]:
    """The command gate (D2) as two views of one evaluation: ``build`` is the
    registry's ``specs`` (translated commands, nothing when fenced) and records
    its receipt; ``declare`` is that receipt, the digest a grant pins and why it
    is fenced, so the wire describes the evaluation that was registered."""
    sdk_specs_of = sdk.specs
    assert sdk_specs_of is not None  # guarded by the caller
    receipt: list[
        tuple[Any, str, str | None]
    ] = []  # (parsed, digest, fenced) of the last build

    def gate(parsed: Any) -> tuple[dict[str, SdkCommandSpec], str, str | None]:
        sdk_specs = dict(sdk_specs_of(parsed))
        digest = command_specs_digest(sdk_specs)
        if CapabilityRequest.COMMAND_CONTRIBUTOR not in grants.effective_capabilities(
            grant
        ):
            return sdk_specs, digest, "command_contributor not granted"
        if digest != grant.command_specs_digest:
            return (
                sdk_specs,
                digest,
                f"command_specs_digest mismatch: built {digest}, "
                f"sealed {grant.command_specs_digest}",
            )
        return sdk_specs, digest, None

    def build(parsed: Any) -> dict[str, CommandSpec]:
        sdk_specs, digest, fenced = gate(parsed)
        receipt[:] = [(parsed, digest, fenced)]
        if fenced is not None:
            logger.warning("external adapter %r: %s; commands fenced", sdk.id, fenced)
            return {}
        return {name: _translate_command_spec(spec) for name, spec in sdk_specs.items()}

    def declare(parsed: Any) -> tuple[str, str | None]:
        if receipt and receipt[0][0] is parsed:
            return receipt[0][1], receipt[0][2]
        _specs, digest, fenced = gate(parsed)
        return digest, fenced

    return build, declare


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
        result=spec.result,
    )


def _translate_param(param: SdkParamDef) -> ParamDef:
    return ParamDef(
        placeholder=param.placeholder,
        default=param.default,
        pattern=param.pattern,
        description=param.description,
        max_bytes=param.max_bytes,
        secret=param.secret,
        schema=param.schema,
    )


def _translate_handler(sdk_handler: SdkCommandHandler) -> Any:
    """Wrap the SDK command handler (a factory returning an async job body over
    the SDK progress/outcome types) into the internal ``CommandHandler`` shape."""

    def factory(params: dict[str, Any]) -> JobHandler | None:
        sdk_job = sdk_handler(params)
        if sdk_job is None:
            return None

        async def run(progress: ProgressCallback) -> JobOutcome:
            outcome = await sdk_job(_ProgressAdapter(progress))
            reason, own_word = _translate_reason(outcome.failure_reason)
            return JobOutcome(
                success=outcome.success,
                exit_code=outcome.exit_code,
                stdout=outcome.stdout,
                stderr=outcome.stderr,
                failure_reason=reason,
                extras=dict(outcome.extras),
                error_code=own_word,
            )

        return run

    return factory


def _translate_reason(reason: str | None) -> tuple[FailureReason | None, str | None]:
    """An adapter's reason the enum knows passes through; one it does not becomes
    ``integration_failed``, the adapter's own word kept as ``error.code``."""
    if reason is None:
        return None, None
    try:
        return FailureReason(reason), None
    except ValueError:
        return FailureReason.INTEGRATION_FAILED, reason


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
