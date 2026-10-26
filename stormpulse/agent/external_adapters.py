"""Entry-layer glue for sealed external adapters (CORE-007): translates what the
loader returns into ``registry.Integration`` (Entry, since a handler builds a
``JobOutcome``). Three gates: an id colliding with a built-in is quarantined
(D6); registration advertises commands only with ``command_contributor`` and a
matching ``command_specs_digest`` (D2); every callback, command and job attempt
re-reads the grant and fails closed (D3). Command-name collision is bootstrap's.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from stormpulse.commands.jobs import JobHandler, JobOutcome, ProgressCallback
from stormpulse.config import CommandSpec, ConfigError, ParamDef
from stormpulse.events import EventSource
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
    SdkEventSource,
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
            register_integration(_translate(sdk, adapter.grant, state_dir))
            registered.add(sdk.id)
        except Exception as exc:  # noqa: BLE001 - one bad adapter must never crash the agent
            logger.warning(
                "external adapter %r failed to register (soft-disabled): %s",
                sdk.id,
                exc,
            )
    return frozenset(registered)


class AdapterFenced(ConfigError):
    """An adapter callback refused because its current grant no longer holds the
    capability. A ConfigError, so every host caller soft-disables on it."""


def _fence(
    state_dir: Path, grant: SealedGrantV1, *needed: CapabilityRequest
) -> Callable[[], str | None]:
    """Why ``needed`` is not in force right now, or None: the grant re-read per call."""

    def refused() -> str | None:
        held = grants.current_capabilities(
            state_dir, grant.integration_id, grant.package_digest
        )
        missing = [c.value for c in needed if c not in held]
        return f"{', '.join(missing)} revoked or not granted" if missing else None

    return refused


def _fenced(fence: Callable[[], str | None], callback: Any) -> Any:
    """``callback`` that runs only while ``fence`` passes; None stays None."""
    if callback is None:
        return None

    def call(*args: Any, **kwargs: Any) -> Any:
        refused = fence()
        if refused is not None:
            raise AdapterFenced(f"external integration is fenced: {refused}")
        return callback(*args, **kwargs)

    return call


def _wrap_parse_config(sdk_parse: Any) -> Any:
    """Map an adapter's config failure to the host's ConfigError, so bootstrap
    soft-disables it (CORE-005 d5 / D6). An adapter can't import ConfigError
    (SDK-only), so it raises SdkConfigError; any other parse failure is treated
    the same rather than crashing the agent."""

    def parse(raw: dict[str, object]) -> Any:
        try:
            return sdk_parse(raw)
        except AdapterFenced:
            raise
        except SdkConfigError as exc:
            raise ConfigError(str(exc)) from exc
        except Exception as exc:
            raise ConfigError(
                "An external integration couldn't read its configuration "
                f"({type(exc).__name__}). Run 'stormpulse integration doctor', "
                "correct its configuration or update the integration, then restart Storm Pulse."
            ) from None

    return parse


def _translate(
    sdk: SdkIntegration, grant: SealedGrantV1, state_dir: Path
) -> Integration:
    load = _fence(state_dir, grant, CapabilityRequest.INTEGRATION_LOAD)
    command = _fence(
        state_dir,
        grant,
        CapabilityRequest.INTEGRATION_LOAD,
        CapabilityRequest.COMMAND_CONTRIBUTOR,
    )
    build, gate = (
        _gated_specs_builder(sdk, grant, load, command)
        if sdk.specs is not None
        else (None, None)
    )
    return Integration(
        id=sdk.id,
        in_release_catalog=False,
        parse_config=_wrap_parse_config(_fenced(load, sdk.parse_config)),
        enabled=_fenced(load, sdk.enabled),
        preconditions=_fenced(load, sdk.preconditions),
        specs=build,
        command_gate=gate,
        capabilities=sdk.capabilities,
        readiness=_fenced(load, sdk.readiness),
        # Needs no command-contributor grant, unlike specs: a deploy subject is
        # data no allow rule binds to, so it moves no digest (CORE-005 D14).
        # The node's own table still overrides whatever it says.
        deploy_subjects=_fenced(load, sdk.deploy_subjects),
        components=(
            _translate_components(_fenced(load, sdk.components))
            if sdk.components is not None
            else None
        ),
        event_source=_event_source(sdk.event_source),
    )


def _event_source(sdk_source: SdkEventSource | None) -> EventSource | None:
    """The declared source as data, under the adapter's activation grant; the
    agent declares it only when it builds the live feeder (``event_spool``)."""
    if sdk_source is None:
        return None
    return EventSource(
        name=sdk_source.source,
        kinds={kind: frozenset(f) for kind, f in sdk_source.kinds.items()},
        spool_dir=Path(sdk_source.spool_dir),
    )


def _translate_components(sdk_components: Any) -> Any:
    """An external adapter holds no state, so its hook sees the config alone."""

    def components(config: Any, _state: Any) -> tuple[Any, ...]:
        return tuple(sdk_components(config))

    return components


def _gated_specs_builder(
    sdk: SdkIntegration,
    grant: SealedGrantV1,
    load: Callable[[], str | None],
    command: Callable[[], str | None],
) -> tuple[Any, Any]:
    """The command gate (D2) as two views of one evaluation: ``build`` is the
    registry's ``specs`` (translated commands, nothing when fenced) and records
    its receipt; ``declare`` is that receipt, the digest a grant pins and why it
    is fenced, so the wire describes the evaluation that was registered."""
    assert sdk.specs is not None  # guarded by the caller
    sdk_specs_of = _fenced(load, sdk.specs)
    receipt: list[
        tuple[Any, str, str | None]
    ] = []  # (parsed, digest, fenced) of the last build

    def gate(parsed: Any) -> tuple[dict[str, SdkCommandSpec], str, str | None]:
        sdk_specs = dict(sdk_specs_of(parsed))
        digest = command_specs_digest(sdk_specs)
        if command() is not None:
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
        return {
            name: _translate_command_spec(spec, command)
            for name, spec in sdk_specs.items()
        }

    def declare(parsed: Any) -> tuple[str, str | None]:
        if receipt and receipt[0][0] is parsed:
            return receipt[0][1], receipt[0][2]
        _specs, digest, fenced = gate(parsed)
        return digest, fenced

    return build, declare


def _translate_command_spec(
    spec: SdkCommandSpec, authorize: Callable[[], str | None]
) -> CommandSpec:
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
        handler=(
            _translate_handler(spec.handler, authorize)
            if spec.handler is not None
            else None
        ),
        params={name: _translate_param(p) for name, p in spec.params.items()},
        result=spec.result,
        retry_window_s=spec.retry_window_s,
        authorize=authorize,
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


def _translate_handler(
    sdk_handler: SdkCommandHandler, authorize: Callable[[], str | None]
) -> Any:
    """Wrap the SDK command handler (a factory returning an async job body over
    the SDK progress/outcome types) into the internal ``CommandHandler`` shape.
    Each attempt re-asks the grant, so a revoke fences queued work and retries."""

    def factory(params: dict[str, Any]) -> JobHandler | None:
        sdk_job = sdk_handler(params)
        if sdk_job is None:
            return None

        async def run(progress: ProgressCallback) -> JobOutcome:
            refused = authorize()
            if refused is not None:
                return JobOutcome(
                    success=False,
                    exit_code=-1,
                    stderr=f"external integration is fenced: {refused}",
                    failure_reason=FailureReason.FORBIDDEN,
                )
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
                retry_after_s=outcome.retry_after_s,
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
