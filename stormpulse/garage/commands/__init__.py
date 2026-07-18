"""Garage-specific whitelisted commands, as single-source CommandSpecs.

The whitelist is split by trust surface, one module per kind:

- ``readonly.py``: read-only CLI diagnostics that mutate nothing.
- ``raw.py``: state-changing single CLI commands, no orchestration.
- ``jobs.py``: orchestrated admin-API / S3 jobs (``mode="job"``), each
  carrying its own lazy handler thunk so there is no separate
  name->factory map to drift against.

Most subprocess specs resolve to ``docker exec <container> /garage
<subcommand>``, shell=False (``mode="subprocess"``).

There is no ``garage_refresh`` here anymore: "refresh my state now" is a
generic, agent-owned capability synthesized for any Integration that declares
``collect_state`` (see ``stormpulse.agent.refresh``), so garage gets it for
free the same way a third-party integration would.

Two pieces of plumbing keep the whitelist scannable instead of a wall of
copy-paste: ``garage_cli(...)`` below writes the ``docker exec <container>
/garage`` prefix once, and ``params.py`` declares the four high-frequency
params once. Declaring a validated param in one place is also the security
win: a wrong-pattern bucket name is unconstructable rather than a copy that
drifted.

The complete command inventory is pinned in one place by
``tests/garage/test_commands.py::test_all_commands_present``: any change to
what the agent is allowed to do shows up as an explicit diff to that list.
"""

from __future__ import annotations

from stormpulse.config import CommandSpec
from stormpulse.garage.commands.jobs import build_job_specs
from stormpulse.garage.commands.raw import build_raw_specs
from stormpulse.garage.commands.readonly import build_readonly_specs
from stormpulse.garage.config import GarageConfig


def build_garage_specs(config: GarageConfig) -> dict[str, CommandSpec]:
    """Build the Garage command registry from config.

    Uses config.docker_binary, config.container_name, and config.garage_binary
    to construct the full subprocess command templates, and binds each job's
    handler thunk to ``config``.
    """
    docker = config.docker_binary
    container = config.container_name
    garage = config.garage_binary

    def garage_cli(*args: str) -> list[str]:
        """The ``docker exec <container> /garage ...`` prefix, written once.

        Subprocess specs pass only their garage subcommand and arguments;
        the docker/exec/container/binary plumbing lives here so each spec
        reads as the garage command it actually runs.
        """
        return [docker, "exec", container, garage, *args]

    specs: dict[str, CommandSpec] = {}
    for group in (
        build_readonly_specs(garage_cli),
        build_raw_specs(garage_cli),
        build_job_specs(config),
    ):
        overlap = specs.keys() & group.keys()
        if overlap:
            raise ValueError(f"duplicate garage command specs: {sorted(overlap)}")
        specs.update(group)
    return specs
