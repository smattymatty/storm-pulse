"""Tests for the register-payload metadata helpers."""

from __future__ import annotations

from stormpulse.agent.metadata import build_commands_metadata, strip_binary_path
from stormpulse.config import CommandDef, ParamDef

from tests.helpers import DUMMY_PROJECT


# ---------------------------------------------------------------------------
# strip_binary_path
# ---------------------------------------------------------------------------


def test_strip_binary_path_absolute() -> None:
    assert strip_binary_path("/usr/bin/docker") == "docker"


def test_strip_binary_path_deep() -> None:
    assert strip_binary_path("/usr/local/bin/git") == "git"


def test_strip_binary_path_relative_unchanged() -> None:
    assert strip_binary_path("python") == "python"


def test_strip_binary_path_single_slash_unchanged() -> None:
    assert strip_binary_path("/single") == "/single"


def test_strip_binary_path_placeholder_unchanged() -> None:
    assert strip_binary_path("{project_dir}") == "{project_dir}"


def test_strip_binary_path_flag_unchanged() -> None:
    assert strip_binary_path("--tail") == "--tail"


# ---------------------------------------------------------------------------
# build_commands_metadata
# ---------------------------------------------------------------------------


def test_build_commands_metadata_basic() -> None:
    registry = {
        "git_pull": CommandDef(
            group="deploy",
            command=["/usr/bin/git", "-C", "{project_dir}", "pull"],
            timeout=60,
            description="Pull latest changes from remote",
        ),
    }
    result = build_commands_metadata(registry, DUMMY_PROJECT)
    assert "git_pull" in result
    entry = result["git_pull"]
    assert entry["group"] == "deploy"
    assert entry["description"] == "Pull latest changes from remote"
    assert entry["template"] == ["git", "-C", "{project_dir}", "pull"]
    assert entry["timeout"] == 60
    assert entry["requires_confirmation"] is False
    assert entry["long_running"] is False
    assert entry["params"] == {}


def test_build_commands_metadata_strips_paths() -> None:
    registry = {
        "docker_up": CommandDef(
            group="deploy",
            command=["/usr/bin/docker", "compose", "up", "-d"],
            timeout=120,
        ),
    }
    result = build_commands_metadata(registry, DUMMY_PROJECT)
    assert result["docker_up"]["template"][0] == "docker"
    assert result["docker_up"]["template"][1] == "compose"


def test_build_commands_metadata_sorted_keys() -> None:
    registry = {
        "z_cmd": CommandDef(group="z", command=["/bin/z"], timeout=10),
        "a_cmd": CommandDef(group="a", command=["/bin/a"], timeout=10),
    }
    result = build_commands_metadata(registry, DUMMY_PROJECT)
    assert list(result.keys()) == ["a_cmd", "z_cmd"]


def test_build_commands_metadata_with_params() -> None:
    registry = {
        "docker_logs": CommandDef(
            group="diagnostics",
            command=["/usr/bin/docker", "logs", "{service}"],
            timeout=30,
            description="Show logs",
            params={
                "service": ParamDef(
                    placeholder="service",
                    default="web",
                    pattern="[a-zA-Z0-9_-]+",
                    description="Docker Compose service name",
                ),
            },
        ),
    }
    result = build_commands_metadata(registry, DUMMY_PROJECT)
    params = result["docker_logs"]["params"]
    assert "service" in params
    assert params["service"] == {
        "default": "web",
        "pattern": "[a-zA-Z0-9_-]+",
        "description": "Docker Compose service name",
    }
    assert "placeholder" not in params["service"]


def test_build_commands_metadata_param_no_default() -> None:
    registry = {
        "logs": CommandDef(
            group="diagnostics",
            command=["/usr/bin/docker", "logs", "{service}"],
            timeout=30,
            params={
                "service": ParamDef(
                    placeholder="service",
                    default=None,
                    pattern="[a-z]+",
                ),
            },
        ),
    }
    result = build_commands_metadata(registry, DUMMY_PROJECT)
    assert result["logs"]["params"]["service"]["default"] is None


def test_build_commands_metadata_param_default_from_config() -> None:
    """Params with no static default get their default from project config."""
    registry = {
        "docker_logs": CommandDef(
            group="diagnostics",
            command=["/usr/bin/docker", "logs", "{docker_service_name}"],
            timeout=30,
            params={
                "docker_service_name": ParamDef(
                    placeholder="docker_service_name",
                    default=None,
                    pattern="[a-zA-Z0-9_-]+",
                    description="Docker Compose service name",
                ),
            },
        ),
    }
    result = build_commands_metadata(registry, DUMMY_PROJECT)
    assert result["docker_logs"]["params"]["docker_service_name"]["default"] == "web"


def test_build_commands_metadata_with_confirmation() -> None:
    registry = {
        "docker_down": CommandDef(
            group="deploy",
            command=["/usr/bin/docker", "compose", "down"],
            timeout=60,
            requires_confirmation=True,
            description="Stop containers",
        ),
    }
    result = build_commands_metadata(registry, DUMMY_PROJECT)
    assert result["docker_down"]["requires_confirmation"] is True


def test_build_commands_metadata_empty_registry() -> None:
    assert build_commands_metadata({}, DUMMY_PROJECT) == {}
