"""The apply environment and the capability-provider seam (P2, CORE-007).

Framework layer. ``ApplyEnv`` carries every host-side handle the engine needs, so
the engine itself imports no Feature and no host global: tests inject fakes, the
CLI injects real ones. The Caddy drop-in is applied through a registered
``CapabilityProvider`` dispatched by token, never a ``caddy/`` import (I13, the
init/orchestrator inversion).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from stormpulse.sdk import CaddyDropIn


class CapabilityProvider(Protocol):
    """A registered handler for a capability-backed mutation (P2: the Caddy
    drop-in). Owns its own capture / forward / verify / compensate so the engine
    never imports the Feature that provides the capability.

    The real ``caddy.drop_in.v1`` provider that mutates an operator's Caddy config
    lands with its first consumer (buckets-gate, P5); P2 proves the seam with a
    synthetic provider.
    """

    def capture(self, mutation: CaddyDropIn, env: ApplyEnv) -> bytes | None:
        """Capture the pre-image (or ``None`` if nothing exists yet)."""
        ...

    def forward(self, mutation: CaddyDropIn, env: ApplyEnv) -> None:
        """Apply the drop-in."""
        ...

    def verify(self, mutation: CaddyDropIn, env: ApplyEnv) -> bool:
        """Confirm the drop-in is written and imported."""
        ...

    def compensate(self, mutation: CaddyDropIn, env: ApplyEnv, pre: bytes | None) -> None:
        """Undo the drop-in (remove it, or restore the pre-image)."""
        ...


@dataclass(slots=True)
class ApplyEnv:
    """Host-side handles for one plan application. The path fields locate the
    host-owned bases the engine may write to; the callables are injected so the
    engine stays free of host globals (and of any Feature import)."""

    config_path: Path
    base_dir: Path
    systemd_user_dir: Path
    # Where the durable apply journal and the wizard-apply lock live (crash
    # recovery, CORE-007). A fresh ``doctor`` process reads the journal here to
    # report or recover an interrupted apply.
    state_dir: Path
    content_store: Mapping[str, bytes] = field(default_factory=dict)
    providers: Mapping[str, CapabilityProvider] = field(default_factory=dict)
    restart: Callable[[str, str], None] | None = None
    health: Callable[[str], bool] | None = None
    probe: Callable[[str], bool] | None = None
    daemon_reload: Callable[[], None] | None = None
    # Post-apply checks the caller injects (service health, dependency re-check).
    # Returns a list of failure reasons; a non-empty list rolls the plan back. The
    # engine always additionally re-parses the config (no host probe) itself.
    post_check: Callable[[], list[str]] | None = None
