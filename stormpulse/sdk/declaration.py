"""The integration declaration surface (CORE-007 runtime loader).

An external adapter declares its integration and commands with these types, and
these types **only** - never the internal ``registry.Integration`` or
``config.CommandSpec``. The loader translates a declared ``SdkIntegration`` into
the internal contract at load time.

Foundation-pure (Fn8): this module imports nothing from ``stormpulse`` beyond
sibling ``stormpulse.sdk`` submodules, and carries no host-mutation primitive.
The shape mirrors ``config.CommandSpec`` / ``config.ParamDef`` /
``commands.jobs.JobOutcome`` in full, and the ``__post_init__`` validations are
mirrored so a private adapter gets the same construction-time footguns the
built-ins do.

``command_specs_digest`` is the single canonical hash both the release-side
author (to fill the manifest) and the host (to verify at load) run, so the
``command_specs_digest`` in a package's manifest cannot drift from what the host
recomputes. It covers the declarative command surface only; the handler code is
pinned by the package digest, not here.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from stormpulse.sdk.readiness import Capability, CapabilityStatus
from stormpulse.sdk.wizard import IntegrationWizard

SdkCommandMode = Literal["subprocess", "job", "refresh"]

_CREDENTIAL_NAME_RE = re.compile(r"secret|password|token|passphrase", re.IGNORECASE)


class SdkConfigError(Exception):
    """Raised by an adapter's ``parse_config`` to soft-disable itself on invalid
    config. The host maps this to its internal config-error type; the adapter and
    every sibling stay up. The adapter never imports the host's config module, so
    this is the SDK-level signal for "this section is misconfigured"."""


@dataclass(frozen=True, slots=True)
class SdkParamDef:
    """An overridable command placeholder. Mirror of ``config.ParamDef``: either
    ``pattern`` or ``max_bytes`` must be set, and a credential-shaped name must
    set ``secret=True`` (which redacts the value from event and log context)."""

    placeholder: str
    default: str | None
    pattern: str | None = None
    description: str = ""
    max_bytes: int | None = None
    secret: bool = False

    def __post_init__(self) -> None:
        if self.pattern is None and self.max_bytes is None:
            raise ValueError(
                f"SdkParamDef {self.placeholder!r}: must set pattern or max_bytes "
                "(unvalidated params are a footgun)"
            )
        if not self.secret and _CREDENTIAL_NAME_RE.search(self.placeholder):
            raise ValueError(
                f"SdkParamDef {self.placeholder!r}: credential-shaped name requires "
                "secret=True (redacts it from event and log context)"
            )


@dataclass(frozen=True, slots=True)
class SdkJobOutcome:
    """Result body a job handler returns. Mirror of ``commands.jobs.JobOutcome``;
    the loader translates it to the internal type."""

    success: bool
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    failure_reason: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


class SdkProgress(Protocol):
    """Progress reporter a job handler is called with. The first call must use
    ``stage="starting"`` with ``current=0``. ``bytes_freed`` is keyword-only for
    space-reclaiming jobs; the byte-moving ``transfer`` channel is deferred until
    an external consumer needs it (no v1 consumer moves bytes through a job)."""

    async def __call__(
        self,
        stage: str,
        current: int,
        total: int | None,
        message: str,
        *,
        bytes_freed: int | None = None,
    ) -> None: ...


# An async job body: given the progress reporter, run and return a typed outcome.
SdkJobHandler = Callable[[SdkProgress], Awaitable[SdkJobOutcome]]

# A "job" command's lazy handler thunk: given validated runtime params, build the
# job handler (or None when unservable on this host). Mirror of the internal
# ``CommandHandler`` factory shape.
SdkCommandHandler = Callable[[dict[str, str]], "SdkJobHandler | None"]


@dataclass(frozen=True, slots=True)
class SdkCommandSpec:
    """A single command an adapter contributes. Full-parity mirror of
    ``config.CommandSpec``. ``mode`` discriminates: ``subprocess`` runs an
    absolute-path argv (``command[0]`` must be absolute); ``job`` carries a
    ``handler``; ``refresh`` is the agent-owned state re-collect and carries no
    handler. Illegal combinations are rejected at construction, exactly as the
    internal spec does."""

    group: str
    command: list[str]
    timeout: int
    mode: SdkCommandMode = "subprocess"
    requires_confirmation: bool = False
    description: str = ""
    sensitive_output: bool = False
    read_only: bool = False
    self_reconciling: bool = False
    handler: SdkCommandHandler | None = None
    params: dict[str, SdkParamDef] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mode == "job":
            if self.handler is None:
                raise ValueError(
                    f"SdkCommandSpec {self.command!r}: mode 'job' requires a handler"
                )
        elif self.handler is not None:
            raise ValueError(
                f"SdkCommandSpec {self.command!r}: mode {self.mode!r} must not carry a handler"
            )
        if self.mode == "subprocess" and (
            not self.command or not self.command[0].startswith("/")
        ):
            raise ValueError(
                f"SdkCommandSpec {self.command!r}: mode 'subprocess' requires an "
                "absolute binary path as command[0]"
            )


@dataclass(frozen=True, slots=True)
class SdkIntegration:
    """A declared external integration (CORE-007). Required core is ``id``,
    ``parse_config``, ``enabled``; every other capability is opt-in. The loader
    translates this into the internal ``registry.Integration``, and exposes
    ``specs`` to command dispatch only when the package holds a sealed
    ``command_contributor`` grant whose ``command_specs_digest`` still matches."""

    id: str
    parse_config: Callable[[dict[str, Any]], Any]
    enabled: Callable[[Any], bool]
    preconditions: Callable[[Any], str | None] | None = None
    specs: Callable[[Any], dict[str, SdkCommandSpec]] | None = None
    capabilities: tuple[Capability, ...] | None = None
    readiness: Callable[[Any], tuple[CapabilityStatus, ...]] | None = None
    # Optional setup wizard (CORE-007 D5). When present, `stormpulse integration
    # init <id>` drives it through the host wizard engine (questions -> inspect ->
    # preview -> transactional apply), so an external adapter is configured with
    # the same quality as a built-in, not hand-edited into stormpulse.toml.
    wizard: IntegrationWizard | None = None


def command_specs_digest(specs: Mapping[str, SdkCommandSpec]) -> str:
    """Canonical ``sha256:`` digest of the declarative command surface.

    Covers every field a control-plane allow rule binds to (name, group, argv,
    timeout, mode, flags, param validators), so any semantic change invalidates
    the grant. Excludes descriptions (cosmetic) and the handler (its code is
    pinned by the package digest). Deterministic: names and params are sorted
    and the JSON is key-sorted and separator-canonical, so the release author
    and the host produce identical bytes."""
    canon = [
        {
            "name": name,
            "group": spec.group,
            "command": list(spec.command),
            "timeout": spec.timeout,
            "mode": spec.mode,
            "requires_confirmation": spec.requires_confirmation,
            "sensitive_output": spec.sensitive_output,
            "read_only": spec.read_only,
            "self_reconciling": spec.self_reconciling,
            "params": {
                pname: {
                    "pattern": pdef.pattern,
                    "max_bytes": pdef.max_bytes,
                    "secret": pdef.secret,
                    "default": pdef.default,
                }
                for pname, pdef in sorted(spec.params.items())
            },
        }
        for name, spec in sorted(specs.items())
    ]
    blob = json.dumps(canon, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()
