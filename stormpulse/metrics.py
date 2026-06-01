"""System metrics collection - CPU, memory, disk, containers."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path

import psutil

from stormpulse.config import Config
from stormpulse.protocol import ContainerInfo, MetricsPayload

logger = logging.getLogger(__name__)

_DOCKER_TIMEOUT = 10


def _parse_container_objects(
    raw_objects: list[dict[str, object]],
) -> list[ContainerInfo]:
    """Convert raw docker compose JSON objects to ContainerInfo."""
    containers: list[ContainerInfo] = []
    for obj in raw_objects:
        containers.append(
            ContainerInfo(
                name=str(obj.get("Name", obj.get("name", "unknown"))),
                status=str(obj.get("State", obj.get("state", "unknown"))),
                image=str(obj.get("Image", obj.get("image", "unknown"))),
            )
        )
    return containers


def _collect_containers(compose_file: Path) -> list[ContainerInfo]:
    """Query Docker Compose for running container status.

    Returns an empty list on any failure - metrics must not crash
    because docker is down or unavailable.
    """
    try:
        proc = subprocess.run(
            [
                "/usr/bin/docker",
                "compose",
                "-f",
                str(compose_file),
                "ps",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=_DOCKER_TIMEOUT,
            shell=False,
        )
    except FileNotFoundError:
        logger.warning("Docker binary not found at /usr/bin/docker")
        return []
    except subprocess.TimeoutExpired:
        logger.warning("Docker compose ps timed out after %ds", _DOCKER_TIMEOUT)
        return []

    if proc.returncode != 0:
        logger.warning(
            "Docker compose ps exited %d: %s", proc.returncode, proc.stderr.strip()
        )
        return []

    output = proc.stdout.strip()
    if not output:
        return []

    # Some Docker Compose versions output a JSON array, others NDJSON.
    # Try array first, fall back to line-by-line.
    try:
        parsed = json.loads(output)
        if isinstance(parsed, list):
            return _parse_container_objects(parsed)
    except json.JSONDecodeError:
        pass

    # NDJSON: one JSON object per line
    objects: list[dict[str, object]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                objects.append(obj)
        except json.JSONDecodeError:
            logger.warning("Skipping unparseable container line: %s", line[:80])
    return _parse_container_objects(objects)


def collect_metrics(config: Config) -> MetricsPayload:
    """Collect current system metrics.

    Uses non-blocking psutil.cpu_percent(interval=None) - call
    prime_cpu_percent() once at agent startup for meaningful values.
    """
    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    load1, load5, _ = os.getloadavg()
    uptime = time.time() - psutil.boot_time()

    containers: list[ContainerInfo] = []
    if config.metrics.collect_containers:
        containers = _collect_containers(config.project.compose_file)

    return MetricsPayload(
        cpu_percent=cpu,
        memory_percent=mem.percent,
        memory_used_mb=mem.used / 1024**2,
        memory_total_mb=mem.total / 1024**2,
        disk_percent=disk.percent,
        disk_used_gb=disk.used / 1024**3,
        disk_total_gb=disk.total / 1024**3,
        load_avg_1m=load1,
        load_avg_5m=load5,
        uptime_seconds=uptime,
        containers=containers,
    )


def prime_cpu_percent() -> None:
    """Prime psutil's CPU percent baseline.

    Without this, the first call to cpu_percent() returns 0.0.
    Call once at agent startup.
    """
    psutil.cpu_percent(interval=None)
