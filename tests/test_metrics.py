"""Tests for stormpulse.metrics."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

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
from stormpulse.metrics import _collect_containers, collect_metrics, prime_cpu_percent

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def config() -> Config:
    return Config(
        agent=AgentConfig(id="test-01", pulse_token="tok-test-123"),
        dashboard=DashboardConfig(
            url="wss://example.com/ws/",
            reconnect_min_seconds=1.0,
            reconnect_max_seconds=30.0,
            heartbeat_interval_seconds=30.0,
        ),
        tls=TlsConfig(
            ca_cert=Path("/tmp/ca.pem"),
            client_cert=Path("/tmp/agent.pem"),
            client_key=Path("/tmp/key.pem"),
        ),
        auth=AuthConfig(hmac_secret=Path("/tmp/hmac.key"), command_max_age_seconds=60),
        metrics=MetricsConfig(push_interval_seconds=15.0, collect_containers=True),
        project=ProjectConfig(
            project_dir=Path("/opt/myapp"),
            compose_file=Path("/opt/myapp/docker-compose.yml"),
            docker_service_name="web",
        ),
        storage=StorageConfig(db_path=Path("/tmp/stormpulse.db")),
    )


@pytest.fixture
def config_no_containers(config: Config) -> Config:
    return Config(
        agent=config.agent,
        dashboard=config.dashboard,
        tls=config.tls,
        auth=config.auth,
        metrics=MetricsConfig(push_interval_seconds=15.0, collect_containers=False),
        project=config.project,
        storage=config.storage,
    )


def _mock_psutil() -> dict[str, Any]:
    """Return mock values for psutil functions."""
    return {
        "cpu_percent": 25.0,
        "virtual_memory": MagicMock(percent=61.2, used=1305670656, total=2147483648),
        "disk_usage": MagicMock(percent=45.0, used=19327352832, total=42949672960),
        "boot_time": 1740100000.0,
    }


NDJSON_OUTPUT = '{"Name":"web","State":"running","Image":"myapp:latest"}\n{"Name":"db","State":"running","Image":"postgres:16"}\n'

JSON_ARRAY_OUTPUT = json.dumps(
    [
        {"Name": "web", "State": "running", "Image": "myapp:latest"},
        {"Name": "db", "State": "running", "Image": "postgres:16"},
    ]
)


# ---------------------------------------------------------------------------
# Metrics collection
# ---------------------------------------------------------------------------


@patch("stormpulse.metrics._collect_containers", return_value=[])
@patch("stormpulse.metrics.os.getloadavg", return_value=(0.75, 0.50, 0.25))
@patch("stormpulse.metrics.time.time", return_value=1740186400.0)
@patch("stormpulse.metrics.psutil")
def test_collect_metrics_returns_payload(
    mock_psutil: MagicMock,
    mock_time: MagicMock,
    mock_loadavg: MagicMock,
    mock_containers: MagicMock,
    config: Config,
) -> None:
    vals = _mock_psutil()
    mock_psutil.cpu_percent.return_value = vals["cpu_percent"]
    mock_psutil.virtual_memory.return_value = vals["virtual_memory"]
    mock_psutil.disk_usage.return_value = vals["disk_usage"]
    mock_psutil.boot_time.return_value = vals["boot_time"]

    metrics = collect_metrics(config)
    assert metrics.cpu_percent == 25.0
    assert metrics.memory_percent == 61.2
    assert metrics.load_avg_1m == 0.75
    assert metrics.load_avg_5m == 0.50


@patch("stormpulse.metrics._collect_containers", return_value=[])
@patch("stormpulse.metrics.os.getloadavg", return_value=(0.75, 0.50, 0.25))
@patch("stormpulse.metrics.time.time", return_value=1740186400.0)
@patch("stormpulse.metrics.psutil")
def test_collect_metrics_memory_mb(
    mock_psutil: MagicMock,
    mock_time: MagicMock,
    mock_loadavg: MagicMock,
    mock_containers: MagicMock,
    config: Config,
) -> None:
    vals = _mock_psutil()
    mock_psutil.cpu_percent.return_value = vals["cpu_percent"]
    mock_psutil.virtual_memory.return_value = vals["virtual_memory"]
    mock_psutil.disk_usage.return_value = vals["disk_usage"]
    mock_psutil.boot_time.return_value = vals["boot_time"]

    metrics = collect_metrics(config)
    expected_used = 1305670656 / 1024**2
    expected_total = 2147483648 / 1024**2
    assert metrics.memory_used_mb == pytest.approx(expected_used)
    assert metrics.memory_total_mb == pytest.approx(expected_total)


@patch("stormpulse.metrics._collect_containers", return_value=[])
@patch("stormpulse.metrics.os.getloadavg", return_value=(0.75, 0.50, 0.25))
@patch("stormpulse.metrics.time.time", return_value=1740186400.0)
@patch("stormpulse.metrics.psutil")
def test_collect_metrics_disk_gb(
    mock_psutil: MagicMock,
    mock_time: MagicMock,
    mock_loadavg: MagicMock,
    mock_containers: MagicMock,
    config: Config,
) -> None:
    vals = _mock_psutil()
    mock_psutil.cpu_percent.return_value = vals["cpu_percent"]
    mock_psutil.virtual_memory.return_value = vals["virtual_memory"]
    mock_psutil.disk_usage.return_value = vals["disk_usage"]
    mock_psutil.boot_time.return_value = vals["boot_time"]

    metrics = collect_metrics(config)
    expected_used = 19327352832 / 1024**3
    expected_total = 42949672960 / 1024**3
    assert metrics.disk_used_gb == pytest.approx(expected_used)
    assert metrics.disk_total_gb == pytest.approx(expected_total)


@patch("stormpulse.metrics._collect_containers", return_value=[])
@patch("stormpulse.metrics.os.getloadavg", return_value=(0.75, 0.50, 0.25))
@patch("stormpulse.metrics.time.time", return_value=1740186400.0)
@patch("stormpulse.metrics.psutil")
def test_collect_metrics_uptime(
    mock_psutil: MagicMock,
    mock_time: MagicMock,
    mock_loadavg: MagicMock,
    mock_containers: MagicMock,
    config: Config,
) -> None:
    vals = _mock_psutil()
    mock_psutil.cpu_percent.return_value = vals["cpu_percent"]
    mock_psutil.virtual_memory.return_value = vals["virtual_memory"]
    mock_psutil.disk_usage.return_value = vals["disk_usage"]
    mock_psutil.boot_time.return_value = vals["boot_time"]

    metrics = collect_metrics(config)
    expected_uptime = 1740186400.0 - 1740100000.0
    assert metrics.uptime_seconds == pytest.approx(expected_uptime)


@patch("stormpulse.metrics._collect_containers")
@patch("stormpulse.metrics.os.getloadavg", return_value=(0.75, 0.50, 0.25))
@patch("stormpulse.metrics.time.time", return_value=1740186400.0)
@patch("stormpulse.metrics.psutil")
def test_collect_metrics_containers_disabled(
    mock_psutil: MagicMock,
    mock_time: MagicMock,
    mock_loadavg: MagicMock,
    mock_containers: MagicMock,
    config_no_containers: Config,
) -> None:
    vals = _mock_psutil()
    mock_psutil.cpu_percent.return_value = vals["cpu_percent"]
    mock_psutil.virtual_memory.return_value = vals["virtual_memory"]
    mock_psutil.disk_usage.return_value = vals["disk_usage"]
    mock_psutil.boot_time.return_value = vals["boot_time"]

    metrics = collect_metrics(config_no_containers)
    mock_containers.assert_not_called()
    assert metrics.containers == []


# ---------------------------------------------------------------------------
# Container collection - NDJSON
# ---------------------------------------------------------------------------


@patch("stormpulse.metrics.subprocess.run")
def test_collect_containers_ndjson(mock_run: MagicMock) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=NDJSON_OUTPUT,
        stderr="",
    )
    containers = _collect_containers(Path("/opt/myapp/docker-compose.yml"))
    assert len(containers) == 2
    assert containers[0].name == "web"
    assert containers[1].name == "db"
    assert containers[0].status == "running"


# ---------------------------------------------------------------------------
# Container collection - JSON array
# ---------------------------------------------------------------------------


@patch("stormpulse.metrics.subprocess.run")
def test_collect_containers_json_array(mock_run: MagicMock) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=JSON_ARRAY_OUTPUT,
        stderr="",
    )
    containers = _collect_containers(Path("/opt/myapp/docker-compose.yml"))
    assert len(containers) == 2
    assert containers[0].name == "web"
    assert containers[1].image == "postgres:16"


# ---------------------------------------------------------------------------
# Container collection - error handling
# ---------------------------------------------------------------------------


@patch("stormpulse.metrics.subprocess.run")
def test_collect_containers_docker_not_found(mock_run: MagicMock) -> None:
    mock_run.side_effect = FileNotFoundError()
    containers = _collect_containers(Path("/opt/myapp/docker-compose.yml"))
    assert containers == []


@patch("stormpulse.metrics.subprocess.run")
def test_collect_containers_timeout(mock_run: MagicMock) -> None:
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=10)
    containers = _collect_containers(Path("/opt/myapp/docker-compose.yml"))
    assert containers == []


@patch("stormpulse.metrics.subprocess.run")
def test_collect_containers_bad_json(mock_run: MagicMock) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="not json at all\n",
        stderr="",
    )
    containers = _collect_containers(Path("/opt/myapp/docker-compose.yml"))
    assert containers == []


@patch("stormpulse.metrics.subprocess.run")
def test_collect_containers_nonzero_exit(mock_run: MagicMock) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="error\n",
    )
    containers = _collect_containers(Path("/opt/myapp/docker-compose.yml"))
    assert containers == []


@patch("stormpulse.metrics.subprocess.run")
def test_collect_containers_empty_output(mock_run: MagicMock) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout="",
        stderr="",
    )
    containers = _collect_containers(Path("/opt/myapp/docker-compose.yml"))
    assert containers == []


@patch("stormpulse.metrics.subprocess.run")
def test_collect_containers_lowercase_keys(mock_run: MagicMock) -> None:
    output = '{"name":"web","state":"up","image":"app:1"}\n'
    mock_run.return_value = subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=output,
        stderr="",
    )
    containers = _collect_containers(Path("/opt/myapp/docker-compose.yml"))
    assert len(containers) == 1
    assert containers[0].name == "web"
    assert containers[0].status == "up"


# ---------------------------------------------------------------------------
# Priming
# ---------------------------------------------------------------------------


@patch("stormpulse.metrics.psutil.cpu_percent")
def test_prime_cpu_percent(mock_cpu: MagicMock) -> None:
    prime_cpu_percent()
    mock_cpu.assert_called_once_with(interval=None)
