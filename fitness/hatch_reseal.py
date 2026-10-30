"""Function 20: every hatch execution rechecks the seal (CORE-004 d6).

Boots a throwaway agent unsealed and records every shell argv ``subprocess.run``
would spawn. Resealed by an earlier sequence step, or before a single request,
no hatch may run. An unsealed control must reach the shell, so a probe that
stops running the hatch for some other reason fails here instead of passing.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import ssl
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from unittest.mock import patch

from fitness.shell_hatches import SHELLS
from stormpulse.agent import Agent, dispatch
from stormpulse.agent.session import SessionCompatibility
from stormpulse.auth import NonceStore, generate_nonce, sign_envelope
from stormpulse.commands.registry import SEALED_COMMANDS
from stormpulse.config import (
    AgentConfig,
    AuthConfig,
    Config,
    DashboardConfig,
    MetricsConfig,
    ProjectConfig,
    StorageConfig,
    TlsConfig,
)
from stormpulse.protocol import SPEAKS, Envelope, MessageType
from stormpulse.signoff import SignoffState

_SECRET = b"fitness-hatch-reseal-secret-key!"
_AGENT_ID = "fitness-reseal"
_DIGEST = "sha256:" + "0f" * 32
_HARMLESS = "git_pull"
_PARAM = {"run_verify_block": "verify_command", "run_apply_block": "apply_command"}


class _Probe:
    """One throwaway agent, booted unsealed, and every shell argv it tried."""

    def __init__(self, state: Path) -> None:
        self.shells: list[list[str]] = []
        self.frames: list[str] = []
        self.reseal_on_harmless = False
        self.nonces = NonceStore(state / "nonces.db")
        SignoffState(state).unseal()  # a fresh state dir is sealed (CORE-004 d6)
        self.agent = Agent(
            _config(state),
            _SECRET,
            self.nonces,
            ssl.create_default_context(),
            asyncio.Event(),
            signoff_state=SignoffState(state),
        )
        self.agent.session_compatibility = SessionCompatibility(_DIGEST, _DIGEST)

    def run(self, argv: list[str], *_a: Any, **_k: Any) -> Any:
        if PurePosixPath(argv[0]).name in SHELLS:
            self.shells.append(argv)
        elif self.reseal_on_harmless:
            self.agent.signoff_state.seal()
        return subprocess.CompletedProcess(argv, 0, "", "")

    async def send(self, frame: str) -> None:
        self.frames.append(frame)

    def dispatch(self, kind: MessageType, payload: dict[str, Any]) -> None:
        frame = Envelope(
            v=SPEAKS[-1],
            type=kind,
            id=str(uuid.uuid4()),
            ts=datetime.now(UTC),
            agent_id=_AGENT_ID,
            payload={**payload, "nonce": generate_nonce()},
        ).to_dict()
        frame["payload"]["hmac"] = sign_envelope(frame, _SECRET)
        raw = json.dumps(frame)
        with patch("subprocess.run", self.run):
            asyncio.run(dispatch.dispatch_message(self.agent, self, raw))  # type: ignore[arg-type]


def check_hatch_rechecks_seal() -> list[str]:
    """Return violation strings; empty list means clean."""
    violations: list[str] = []
    with _quiet():
        for hatch in sorted(SEALED_COMMANDS):
            violations += _sequence_half(hatch)
            violations += _single_half(hatch)
    return violations


def _sequence_half(hatch: str) -> list[str]:
    violations: list[str] = []
    for reseal, stop_on_failure in itertools.product((False, True), (True, False)):
        with _probe() as probe:
            probe.reseal_on_harmless = reseal
            probe.dispatch(
                MessageType.COMMAND_SEQUENCE,
                {
                    "sequence_id": str(uuid.uuid4()),
                    "steps": [
                        {"command": _HARMLESS, "params": {}},
                        {"command": hatch, "params": {_PARAM[hatch]: "true"}},
                    ],
                    "stop_on_failure": stop_on_failure,
                },
            )
            violations += _verdict(
                probe,
                resealed=reseal,
                what=f"sequence {hatch} (stop_on_failure={stop_on_failure})",
                when="after an earlier step resealed",
            )
    return violations


def _single_half(hatch: str) -> list[str]:
    violations: list[str] = []
    for reseal in (False, True):
        with _probe() as probe:
            if reseal:
                probe.agent.signoff_state.seal()
            probe.dispatch(
                MessageType.COMMAND_REQUEST,
                {"command": hatch, "params": {_PARAM[hatch]: "true"}},
            )
            violations += _verdict(
                probe,
                resealed=reseal,
                what=f"request {hatch}",
                when="after a reseal before dispatch",
            )
    return violations


def _verdict(probe: _Probe, *, resealed: bool, what: str, when: str) -> list[str]:
    if resealed and probe.shells:
        return [f"{what}: spawned {probe.shells[0][0]} {when}"]
    if not resealed and not probe.shells:
        return [f"{what}: unsealed control never reached the shell; probe is blind"]
    return []


@contextmanager
def _probe() -> Iterator[_Probe]:
    with tempfile.TemporaryDirectory(prefix="fitness-reseal-") as tmp:
        probe = _Probe(Path(tmp))
        try:
            yield probe
        finally:
            probe.nonces.close()


@contextmanager
def _quiet() -> Iterator[None]:
    """Dispatch logs every refusal at WARNING; the harness prints its own verdict."""
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous)


def _config(state: Path) -> Config:
    return Config(
        agent=AgentConfig(id=_AGENT_ID, pulse_token="fitness"),
        dashboard=DashboardConfig(
            url="ws://localhost:0/ws/",
            reconnect_min_seconds=1,
            reconnect_max_seconds=1,
            heartbeat_interval_seconds=1,
        ),
        tls=TlsConfig(
            ca_cert=state / "ca.pem",
            client_cert=state / "agent.pem",
            client_key=state / "key.pem",
        ),
        auth=AuthConfig(hmac_secret=state / "hmac.key", command_max_age_seconds=60),
        metrics=MetricsConfig(push_interval_seconds=60, collect_containers=False),
        project=ProjectConfig(
            project_dir=state,
            compose_file=state / "docker-compose.yml",
            docker_service_name="web",
        ),
        storage=StorageConfig(db_path=state / "nonces.db"),
    )
