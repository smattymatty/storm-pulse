"""Subprocess helper for invoking the Garage CLI via ``docker exec``."""

from __future__ import annotations

import asyncio

from stormpulse.garage.config import GarageConfig

# Per-step subprocess timeout. Garage CLI calls are typically <1s; the
# generous bound covers cluster-load spikes without letting a hung call
# block the whole job indefinitely.
_STEP_TIMEOUT_SECONDS = 30


async def run_garage(
    garage_config: GarageConfig,
    *args: str,
    timeout: float = _STEP_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    """Run ``docker exec <container> /garage <args>``.

    Returns ``(returncode, stdout, stderr)``. On timeout, the subprocess
    is killed and ``TimeoutError`` propagates.
    """
    cmd = [
        garage_config.docker_binary,
        "exec",
        garage_config.container_name,
        garage_config.garage_binary,
        *args,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return (
        proc.returncode if proc.returncode is not None else -1,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )
