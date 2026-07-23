"""The runtime loader: the ONE module in this subpackage that executes sealed
package code (CORE-007).

Everything else under ``integrations/external/`` is no-execution (Fn7); this
module is the sanctioned executor. It imports a sealed, active-granted adapter
from its immutable content-addressed tree through a scoped ``MetaPathFinder``
(never ``sys.path`` - D1), after re-hashing the tree against the sealed digest.

It returns the loaded ``SdkIntegration`` objects and their grants. Translating
them into the internal registry contract happens in the Entry layer, not here:
translating a command handler constructs a ``commands.jobs.JobOutcome``, and the
CORE-000 four-layer topology forbids ``integrations/`` from importing
``commands/``.

Per-adapter failures are isolated: one bad package soft-disables itself and
never blocks the others or the agent.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import logging
import sys
from dataclasses import dataclass
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType

from stormpulse.integrations.external import digest, grants, layout, manifest
from stormpulse.integrations.external.model import SealedGrantV1
from stormpulse.sdk import SdkIntegration

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LoadedAdapter:
    grant: SealedGrantV1
    integration: SdkIntegration


class _SealedFinder(importlib.abc.MetaPathFinder):
    """Resolves sealed integration-ids to their content-addressed tree. Fires
    only for ids it owns (each unique + non-stdlib + non-stormpulse by the
    reserved-id rule), so it can shadow nothing, and it never touches sys.path."""

    def __init__(self) -> None:
        self._roots: dict[str, Path] = {}

    def register(self, integration_id: str, package_root: Path) -> None:
        self._roots[integration_id] = package_root

    def find_spec(
        self,
        fullname: str,
        path: object = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        root = self._roots.get(fullname.split(".", 1)[0])
        if root is None:
            return None
        rel = fullname.replace(".", "/")
        package_init = root / rel / "__init__.py"
        if package_init.is_file():
            return importlib.util.spec_from_file_location(
                fullname, package_init, submodule_search_locations=[str(root / rel)]
            )
        module_file = root / f"{rel}.py"
        if module_file.is_file():
            return importlib.util.spec_from_file_location(fullname, module_file)
        return None


_FINDER = _SealedFinder()
_FINDER_INSTALLED = False


def _ensure_finder() -> None:
    global _FINDER_INSTALLED
    if not _FINDER_INSTALLED:
        sys.meta_path.append(_FINDER)  # append: our finder is the last resort, never a shadow
        _FINDER_INSTALLED = True


def load_sealed_adapters(state_dir: Path) -> list[LoadedAdapter]:
    """Import every sealed, active-granted adapter. A per-adapter failure is
    logged and skipped (soft-disable); it never propagates."""
    _ensure_finder()
    loaded: list[LoadedAdapter] = []
    for integration_id in grants.active_integration_ids(state_dir):
        grant = grants.active_grant(state_dir, integration_id)
        if grant is None:
            continue
        try:
            loaded.append(_load_one(state_dir, grant))
        except Exception as exc:  # noqa: BLE001 - one bad adapter must never crash the agent
            logger.warning(
                "external adapter %r failed to load (soft-disabled): %s", integration_id, exc
            )
    return loaded


def load_one_sealed_adapter(state_dir: Path, integration_id: str) -> LoadedAdapter | None:
    """Load a single sealed adapter by id (for `integration init`). Returns None
    if it has no active grant; raises with a clear reason if it is granted but
    unloadable, so the caller can report it rather than silently skipping."""
    _ensure_finder()
    grant = grants.active_grant(state_dir, integration_id)
    if grant is None:
        return None
    return _load_one(state_dir, grant)


def _load_one(state_dir: Path, grant: SealedGrantV1) -> LoadedAdapter:
    installed = layout.packages_dir(state_dir) / grant.package_digest.split(":", 1)[1]
    if not installed.is_dir():
        raise RuntimeError("installed package tree is missing")

    # Re-hash the immutable tree against the sealed digest before importing:
    # authority is bound to the digest, so a tampered or corrupt tree loads nothing.
    scan = digest.scan_and_hash(installed)
    if scan.package_digest != grant.package_digest:
        raise RuntimeError("installed tree does not match the sealed digest (tampered or corrupt)")
    if scan.manifest_bytes is None:
        raise RuntimeError("installed package has no manifest")

    parsed = manifest.parse_manifest(scan.manifest_bytes)
    _FINDER.register(grant.integration_id, installed)
    module = importlib.import_module(parsed.entry_module)
    entry = getattr(module, parsed.entry_object, None)
    if not isinstance(entry, SdkIntegration):
        raise RuntimeError(f"entry object {parsed.entry_object!r} is not an SdkIntegration")
    if entry.id != grant.integration_id:
        raise RuntimeError(
            f"adapter declares id {entry.id!r} but was sealed as {grant.integration_id!r}"
        )
    return LoadedAdapter(grant=grant, integration=entry)
