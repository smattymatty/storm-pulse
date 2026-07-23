"""The runtime loader (CORE-007): a real sealed adapter is imported through the
scoped MetaPathFinder (no sys.path), translated, and registered - with the
command gate, id-collision quarantine, and per-adapter soft-disable enforced."""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import stormpulse.integrations.registry as reg
from stormpulse.agent.external_adapters import load_and_register_external
from stormpulse.integrations import Integration, register_integration
from stormpulse.integrations.external import digest, grants, install, trust
from stormpulse.integrations.external.model import CapabilityRequest
from stormpulse.sdk import (
    SdkCommandSpec,
    SdkJobOutcome,
    SdkParamDef,
    command_specs_digest,
)

from ._helpers import approve, keypair, state_dir

_AGENT = "agent-1"


@pytest.fixture(autouse=True)
def _isolate() -> object:
    """Restore the global registry and sys.modules so loader tests don't leak
    registered adapters or imported adapter modules into each other."""
    saved_integrations = list(reg._integrations)
    saved_modules = set(sys.modules)
    yield
    reg._integrations[:] = saved_integrations
    for name in set(sys.modules) - saved_modules:
        del sys.modules[name]


def _adapter_source(integration_id: str, *, raise_on_import: bool = False) -> str:
    if raise_on_import:
        return "raise RuntimeError('boom at import')\n"
    return (
        "from stormpulse.sdk import SdkIntegration, SdkCommandSpec, SdkParamDef, SdkJobOutcome\n"
        "\n"
        "async def _do(progress):\n"
        "    await progress('starting', 0, 1, 'go')\n"
        "    return SdkJobOutcome(success=True, stdout='did it')\n"
        "\n"
        "def _specs(config):\n"
        f"    return {{'{integration_id}_do': SdkCommandSpec(\n"
        f"        group='{integration_id}', command=['{integration_id}_do'], timeout=30, mode='job',\n"
        "        handler=lambda params: _do,\n"
        "        params={'x': SdkParamDef(placeholder='x', default=None, max_bytes=100)})}\n"
        "\n"
        "INTEGRATION = SdkIntegration(\n"
        f"    id='{integration_id}',\n"
        "    parse_config=lambda section: section,\n"
        "    enabled=lambda cfg: True,\n"
        "    specs=_specs,\n"
        ")\n"
    )


def _expected_digest(integration_id: str) -> str:
    async def _noop(_p: object) -> SdkJobOutcome:
        return SdkJobOutcome(success=True)

    specs = {
        f"{integration_id}_do": SdkCommandSpec(
            group=integration_id,
            command=[f"{integration_id}_do"],
            timeout=30,
            mode="job",
            handler=lambda _params: _noop,
            params={"x": SdkParamDef(placeholder="x", default=None, max_bytes=100)},
        )
    }
    return command_specs_digest(specs)


def _install_and_seal(
    tmp_path: Path,
    state: Path,
    private: Ed25519PrivateKey,
    fingerprint: str,
    *,
    integration_id: str,
    capabilities: tuple[str, ...] = ("integration_load", "command_contributor"),
    command_specs_digest_value: str | None = None,
    raise_on_import: bool = False,
) -> str:
    pkg = tmp_path / f"src-{integration_id}"
    (pkg / integration_id).mkdir(parents=True)
    (pkg / integration_id / "__init__.py").write_bytes(b"")
    (pkg / integration_id / "integration.py").write_text(
        _adapter_source(integration_id, raise_on_import=raise_on_import)
    )

    caps = ", ".join(f'"{c}"' for c in capabilities)
    lines = [
        "schema_version = 1",
        "",
        "[integration]",
        f'id = "{integration_id}"',
        'version = "1.0.0"',
        f'entry_module = "{integration_id}.integration"',
        "",
        "[publisher]",
        f'fingerprint = "{fingerprint}"',
        "",
        "[requests]",
        f"capabilities = [{caps}]",
    ]
    if "command_contributor" in capabilities:
        csd = command_specs_digest_value or _expected_digest(integration_id)
        lines.append(f'command_specs_digest = "{csd}"')
    (pkg / digest.MANIFEST_NAME).write_bytes(("\n".join(lines) + "\n").encode())

    package_digest = digest.scan_and_hash(pkg).package_digest
    payload = trust.signed_payload(package_digest, integration_id, "1.0.0")
    (pkg / digest.SIGNATURE_NAME).write_bytes(
        json.dumps(
            {
                "schema_version": 1,
                "algorithm": "ed25519",
                "publisher_fingerprint": fingerprint,
                "package_digest": package_digest,
                "signature_b64": base64.b64encode(private.sign(payload)).decode("ascii"),
            }
        ).encode()
    )
    installed = install.commit_install(pkg, state_dir=state, agent_id=_AGENT).package_digest
    grants.seal(state, package_digest=installed)
    return installed


def _registered(integration_id: str) -> Integration | None:
    return next((i for i in reg.registered_integrations() if i.id == integration_id), None)


# ---------------------------------------------------------------------------


def test_loads_translates_and_registers_with_commands(tmp_path: Path) -> None:
    private, fp = keypair()
    state = state_dir(tmp_path)
    approve(state, tmp_path, private)
    _install_and_seal(tmp_path, state, private, fp, integration_id="advalid")

    before = list(sys.path)
    ids = load_and_register_external(state)
    assert sys.path == before, "the loader must never mutate sys.path (D1)"

    assert "advalid" in ids
    integ = _registered("advalid")
    assert integ is not None and integ.specs is not None
    built = integ.specs({})  # gate passes: command_contributor granted + digest matches
    assert "advalid_do" in built
    assert built["advalid_do"].group == "advalid"


def test_command_contributor_revoked_loads_command_less(tmp_path: Path) -> None:
    private, fp = keypair()
    state = state_dir(tmp_path)
    approve(state, tmp_path, private)
    d = _install_and_seal(tmp_path, state, private, fp, integration_id="adfenced")
    grants.revoke(state, package_digest=d, capability=CapabilityRequest.COMMAND_CONTRIBUTOR)

    ids = load_and_register_external(state)
    assert "adfenced" in ids  # still loads for state/health
    integ = _registered("adfenced")
    assert integ is not None and integ.specs is not None
    assert integ.specs({}) == {}  # commands fenced


def test_digest_mismatch_loads_command_less(tmp_path: Path) -> None:
    private, fp = keypair()
    state = state_dir(tmp_path)
    approve(state, tmp_path, private)
    # Manifest declares a command_specs_digest that does not match the code.
    _install_and_seal(
        tmp_path, state, private, fp, integration_id="adliar",
        command_specs_digest_value="sha256:" + "0" * 64,
    )
    ids = load_and_register_external(state)
    assert "adliar" in ids
    integ = _registered("adliar")
    assert integ is not None and integ.specs is not None
    assert integ.specs({}) == {}  # mismatch => commands fenced, adapter still loads


def test_id_collision_with_builtin_is_quarantined(tmp_path: Path) -> None:
    private, fp = keypair()
    state = state_dir(tmp_path)
    approve(state, tmp_path, private)
    # A stand-in built-in owns the id first.
    register_integration(Integration(id="collideme", parse_config=lambda s: s, enabled=lambda c: True))
    _install_and_seal(tmp_path, state, private, fp, integration_id="collideme")

    ids = load_and_register_external(state)
    assert "collideme" not in ids  # external quarantined, built-in wins
    # the built-in stand-in is still the one registered (no specs)
    builtin = _registered("collideme")
    assert builtin is not None and builtin.specs is None


def test_import_error_soft_disables(tmp_path: Path) -> None:
    private, fp = keypair()
    state = state_dir(tmp_path)
    approve(state, tmp_path, private)
    _install_and_seal(
        tmp_path, state, private, fp, integration_id="adboom",
        capabilities=("integration_load",), raise_on_import=True,
    )
    ids = load_and_register_external(state)  # must not raise
    assert "adboom" not in ids
    assert _registered("adboom") is None


# --- command-name collision (D6, enforced in bootstrap's table assembly) ------


def test_external_command_name_collision_quarantines() -> None:
    from stormpulse.agent.bootstrap import _resolve_integration
    from stormpulse.agent.integrations_runtime import STATUS_DISABLED_ERROR
    from stormpulse.config import CommandSpec

    # A built-in already owns "shared_cmd".
    commands = {"shared_cmd": CommandSpec(group="builtin", command=["/bin/true"], timeout=5)}

    def specs(_cfg: object) -> dict[str, CommandSpec]:
        return {"shared_cmd": CommandSpec(group="ext", command=["/bin/true"], timeout=5)}

    integ = Integration(id="ext", parse_config=lambda s: s, enabled=lambda c: True, specs=specs)
    runtime = _resolve_integration(integ, {}, commands, frozenset({"ext"}))

    assert runtime.status == STATUS_DISABLED_ERROR
    assert "collide" in (runtime.disabled_reason or "")
    assert commands["shared_cmd"].group == "builtin"  # built-in untouched, external lost


def test_wrap_parse_config_maps_adapter_errors_to_config_error() -> None:
    from stormpulse.agent.external_adapters import _wrap_parse_config
    from stormpulse.config import ConfigError
    from stormpulse.sdk import SdkConfigError

    def bad(_raw: dict[str, object]) -> object:
        raise SdkConfigError("policy_path must be absolute")

    with pytest.raises(ConfigError):
        _wrap_parse_config(bad)({})

    def boom(_raw: dict[str, object]) -> object:
        raise ValueError("unexpected")

    with pytest.raises(ConfigError):  # any parse failure soft-disables, never crashes
        _wrap_parse_config(boom)({})
