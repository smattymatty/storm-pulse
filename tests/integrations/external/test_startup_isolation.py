"""Agent startup is inert to external-loader state (T29) and P1 operations never
touch the built-in registry (T30).

The external loader is a local operator surface with no runtime coupling in P1:
the agent bootstrap does not read the integrations state tree and does not import
any installed package. These tests pin that separation empirically, so a later
phase cannot quietly wire loading into startup without turning one of them red.
"""

from __future__ import annotations

import json
from pathlib import Path

from stormpulse.agent.bootstrap import build_agent_dependencies
from stormpulse.integrations.external import doctor, inspection, install, layout, ledger, trust
from stormpulse.integrations.registry import registered_integrations
from tests.integrations.external._helpers import (
    approve as _approve,
    keypair as _keypair,
    make_package as _make_package,
    state_dir as _state,
)
from tests.helpers import build_config


def _seed_malicious_state(state: Path) -> Path:
    """Write hostile P1 state: a corrupt receipt, a fake package tree whose entry
    module would create a marker if it were ever imported, and a garbage publisher
    record. Returns the marker path that must never come into existence."""
    layout.ensure_layout(state)
    marker = state / "SENTINEL_IMPORTED"
    fake_hex = "de" * 32
    pkg = layout.packages_dir(state) / fake_hex
    pkg.mkdir(parents=True)
    (pkg / "code.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('x')\n")
    corrupt = layout.receipts_dir(state) / "obs"
    corrupt.mkdir(parents=True)
    (corrupt / f"{fake_hex}.json").write_text("{ this is not valid json")
    (layout.publishers_dir(state) / f"sha256_{fake_hex}.json").write_text(json.dumps({"garbage": True}))
    return marker


def _registry(cfg: object) -> set[str]:
    deps = build_agent_dependencies(cfg, signoff_sealed=False, log_position_store=None)  # type: ignore[arg-type]
    return set(deps.registry)


def test_t29_malicious_p1_state_leaves_bootstrap_unchanged(tmp_path: Path) -> None:
    cfg = build_config(tmp_path)  # external state dir is db_path.parent == tmp_path
    baseline = _registry(cfg)

    marker = _seed_malicious_state(tmp_path)
    after = _registry(cfg)

    assert after == baseline  # startup registry is byte-for-byte unaffected
    assert not marker.exists()  # no installed package code was ever imported


def test_t30_builtin_registry_unchanged_across_p1_ops(tmp_path: Path) -> None:
    cfg = build_config(tmp_path)
    integrations_before = [i.id for i in registered_integrations()]
    registry_before = _registry(cfg)

    # A full P1 lifecycle: approve, inspect, install, list, doctor, revoke.
    private, fingerprint = _keypair()
    state = _state(tmp_path)  # loader state, separate from the agent's config dir
    _approve(state, tmp_path, private)
    src = tmp_path / "src"
    _make_package(src, private, fingerprint)
    inspection.inspect_package(src, state)
    install.commit_install(src, state_dir=state, agent_id="a")
    ledger.list_receipts(state)
    doctor.doctor_packages(state)
    trust.revoke_publisher(state, fingerprint)

    assert [i.id for i in registered_integrations()] == integrations_before
    assert _registry(cfg) == registry_before
