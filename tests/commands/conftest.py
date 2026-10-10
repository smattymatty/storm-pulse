"""Fixtures shared by the command registry tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from stormpulse.config import ProjectConfig


@pytest.fixture
def project_config() -> ProjectConfig:
    return ProjectConfig(
        project_dir=Path("/opt/myapp"),
        compose_file=Path("/opt/myapp/docker-compose.yml"),
        docker_service_name="web",
        env_file=Path("/opt/myapp/.env"),
    )
