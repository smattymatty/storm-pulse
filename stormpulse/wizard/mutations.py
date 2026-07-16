"""Per-mutation forward / verify / inverse (the CORE-007 D5 inverse table in code).

Framework layer. Each mutation kind builds a ``Step`` whose pre-image is captured
*before* any host change (I3). ``build_step`` never mutates the host; the returned
closures do. Best-effort kinds (systemd, caddy, restart) are marked ``atomic=False``
so the engine's preview can label them honestly (I6).

Each step also carries the raw pre-image bytes and a ``recover_path``/``recover_mode``
for the file-based kinds, so a fresh process (``doctor`` after a crash) can restore
the pre-apply state from the durable journal without the original process's memory.
File-based kinds are recoverable out-of-process; provider- and service-manager kinds
(caddy, restart) are recorded but marked not auto-recoverable.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from stormpulse.sdk import (
    CaddyDropIn,
    ClaimTomlSection,
    CreateSystemdUserUnit,
    InstallBinary,
    InstallFile,
    Mutation,
    MutationKind,
    RestartOrReload,
    VerifyProbe,
    mutation_kind,
)
from stormpulse.wizard.env import ApplyEnv
from stormpulse.wizard.errors import WizardError
from stormpulse.wizard.toml_edit import (
    atomic_write_bytes,
    claim_section,
    read_bytes_or_none,
    restore_or_remove,
    section_equals,
)

_UNIT_RE = re.compile(r"^[a-z0-9@._-]+\.service$")
_CADDY_CAPABILITY = "caddy.drop_in.v1"


@dataclass(slots=True)
class Step:
    """One built mutation step: metadata, captured recovery state, and closures.

    ``recover_path`` (with ``pre_image`` and ``recover_mode``) is set for the
    file-based kinds a fresh process can restore out-of-band; it is ``None`` for
    provider/service-manager kinds that need the original process or a provider.
    """

    kind: MutationKind
    target: str
    atomic: bool
    pre_image: bytes | None
    pre_image_digest: str | None
    recover_path: str | None
    recover_mode: int
    forward: Callable[[], None]
    verify: Callable[[], bool]
    compensate: Callable[[], None]


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _resolve_under(base: Path, rel_target: str) -> Path:
    """Resolve ``rel_target`` under a host-owned base, rejecting escape."""
    if not rel_target or rel_target.startswith("/") or "\\" in rel_target or "\x00" in rel_target:
        raise WizardError(f"illegal install target {rel_target!r}")
    parts = Path(rel_target).parts
    if any(part in (".", "..") for part in parts):
        raise WizardError(f"install target must not contain . or .. : {rel_target!r}")
    resolved = (base / rel_target).resolve()
    if base.resolve() not in resolved.parents and resolved != base.resolve():
        raise WizardError(f"install target escapes base: {rel_target!r}")
    return resolved


def _build_claim_toml(m: ClaimTomlSection, env: ApplyEnv) -> Step:
    pre = read_bytes_or_none(env.config_path)
    content = dict(m.content)

    def forward() -> None:
        claim_section(env.config_path, m.section, content)

    def verify() -> bool:
        return section_equals(env.config_path, m.section, content)

    def compensate() -> None:
        restore_or_remove(env.config_path, pre)

    return Step(
        kind=MutationKind.CLAIM_TOML_SECTION,
        target=m.section,
        atomic=True,
        pre_image=pre,
        pre_image_digest=_sha256(pre) if pre is not None else None,
        recover_path=str(env.config_path),
        recover_mode=0o644,
        forward=forward,
        verify=verify,
        compensate=compensate,
    )


def _build_install(
    m: InstallFile | InstallBinary, env: ApplyEnv, *, kind: MutationKind
) -> Step:
    target = _resolve_under(env.base_dir, m.rel_target)
    data = env.content_store.get(m.content_digest)
    if data is None:
        raise WizardError(f"no content for digest {m.content_digest}")
    if _sha256(data) != m.content_digest:
        raise WizardError(f"content does not match digest {m.content_digest}")
    pre = read_bytes_or_none(target)
    mode = m.mode

    def forward() -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(target, data, mode)

    def verify() -> bool:
        if not target.is_file():
            return False
        if _sha256(target.read_bytes()) != m.content_digest:
            return False
        return (target.stat().st_mode & 0o777) == mode

    def compensate() -> None:
        restore_or_remove(target, pre, mode)

    return Step(
        kind=kind,
        target=m.rel_target,
        atomic=True,
        pre_image=pre,
        pre_image_digest=_sha256(pre) if pre is not None else None,
        recover_path=str(target),
        recover_mode=mode,
        forward=forward,
        verify=verify,
        compensate=compensate,
    )


def _build_systemd_unit(m: CreateSystemdUserUnit, env: ApplyEnv) -> Step:
    if not _UNIT_RE.match(m.unit_name):
        raise WizardError(f"illegal unit name {m.unit_name!r}")
    target = env.systemd_user_dir / m.unit_name
    pre = read_bytes_or_none(target)
    body = m.content.encode("utf-8")

    def forward() -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(target, body, 0o644)
        if env.daemon_reload is not None:
            env.daemon_reload()

    def verify() -> bool:
        return target.is_file() and target.read_bytes() == body

    def compensate() -> None:
        restore_or_remove(target, pre)
        if env.daemon_reload is not None:
            env.daemon_reload()

    return Step(
        kind=MutationKind.CREATE_SYSTEMD_USER_UNIT,
        target=m.unit_name,
        atomic=False,  # daemon-reload is not transactional
        pre_image=pre,
        pre_image_digest=_sha256(pre) if pre is not None else None,
        recover_path=str(target),  # the unit file is restorable; a stale reload is not
        recover_mode=0o644,
        forward=forward,
        verify=verify,
        compensate=compensate,
    )


def _build_caddy_drop_in(m: CaddyDropIn, env: ApplyEnv) -> Step:
    provider = env.providers.get(_CADDY_CAPABILITY)
    if provider is None:
        raise WizardError(
            f"no provider registered for {_CADDY_CAPABILITY} "
            "(the real provider lands with buckets-gate, P5)"
        )
    pre = provider.capture(m, env)

    def forward() -> None:
        provider.forward(m, env)

    def verify() -> bool:
        return provider.verify(m, env)

    def compensate() -> None:
        provider.compensate(m, env, pre)

    return Step(
        kind=MutationKind.CADDY_DROP_IN,
        target=m.drop_in_name,
        atomic=False,  # proxy reload is not transactional
        pre_image=pre,
        pre_image_digest=_sha256(pre) if pre is not None else None,
        recover_path=None,  # provider-managed; a fresh process cannot compensate alone
        recover_mode=0,
        forward=forward,
        verify=verify,
        compensate=compensate,
    )


def _build_restart(m: RestartOrReload, env: ApplyEnv) -> Step:
    if m.action not in ("restart", "reload"):
        raise WizardError(f"illegal restart action {m.action!r}")

    def forward() -> None:
        if env.restart is None:
            raise WizardError("no restart handler configured")
        env.restart(m.unit, m.action)

    def verify() -> bool:
        return env.health(m.unit) if env.health is not None else True

    def compensate() -> None:
        # Best-effort: a service manager is not transactional and the prior active
        # state is not captured. Nothing to restore; a failure surfaces loudly via
        # the engine, it is not silently swallowed.
        return None

    return Step(
        kind=MutationKind.RESTART_OR_RELOAD,
        target=m.unit,
        atomic=False,
        pre_image=None,
        pre_image_digest=None,
        recover_path=None,
        recover_mode=0,
        forward=forward,
        verify=verify,
        compensate=compensate,
    )


def _build_verify_probe(m: VerifyProbe, env: ApplyEnv) -> Step:
    def forward() -> None:
        return None  # read-only

    def verify() -> bool:
        return env.probe(m.capability) if env.probe is not None else True

    def compensate() -> None:
        return None

    return Step(
        kind=MutationKind.VERIFY_PROBE,
        target=m.capability,
        atomic=True,
        pre_image=None,
        pre_image_digest=None,
        recover_path=None,
        recover_mode=0,
        forward=forward,
        verify=verify,
        compensate=compensate,
    )


def build_step(mutation: Mutation, env: ApplyEnv) -> Step:
    """Build the step for one mutation, capturing its pre-image. Does NOT mutate
    the host; the returned ``forward`` does. Raises ``WizardError`` on an invalid
    mutation before any change (F5)."""
    if isinstance(mutation, ClaimTomlSection):
        return _build_claim_toml(mutation, env)
    if isinstance(mutation, InstallFile):
        return _build_install(mutation, env, kind=MutationKind.INSTALL_FILE)
    if isinstance(mutation, InstallBinary):
        return _build_install(mutation, env, kind=MutationKind.INSTALL_BINARY)
    if isinstance(mutation, CreateSystemdUserUnit):
        return _build_systemd_unit(mutation, env)
    if isinstance(mutation, CaddyDropIn):
        return _build_caddy_drop_in(mutation, env)
    if isinstance(mutation, RestartOrReload):
        return _build_restart(mutation, env)
    if isinstance(mutation, VerifyProbe):
        return _build_verify_probe(mutation, env)
    raise WizardError(f"unknown mutation kind {mutation_kind(mutation)!r}")
