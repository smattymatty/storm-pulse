"""Storm Pulse configuration — TOML loading and validation."""

from __future__ import annotations

import dataclasses
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """Raised when configuration is missing, invalid, or incomplete."""


# ---------------------------------------------------------------------------
# Config dataclasses — one per TOML section
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentConfig:
    id: str
    pulse_token: str
    disabled_commands: frozenset[str] = dataclasses.field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class DashboardConfig:
    url: str
    reconnect_min_seconds: float
    reconnect_max_seconds: float
    heartbeat_interval_seconds: float


@dataclass(frozen=True, slots=True)
class TlsConfig:
    ca_cert: Path
    client_cert: Path
    client_key: Path


@dataclass(frozen=True, slots=True)
class AuthConfig:
    hmac_secret: Path
    command_max_age_seconds: int


@dataclass(frozen=True, slots=True)
class MetricsConfig:
    push_interval_seconds: float
    collect_containers: bool


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    project_dir: Path
    compose_file: Path
    docker_service_name: str
    env_file: Path | None = None


@dataclass(frozen=True, slots=True)
class StorageConfig:
    db_path: Path


@dataclass(frozen=True, slots=True)
class ParamDef:
    """Declares an overridable placeholder for a command."""

    placeholder: str
    default: str | None
    pattern: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class CommandDef:
    """A single whitelisted command definition."""

    group: str
    command: list[str]
    timeout: int
    requires_confirmation: bool = False
    description: str = ""
    params: dict[str, ParamDef] = dataclasses.field(default_factory=dict)


PROTECTED_PLACEHOLDERS: frozenset[str] = frozenset({
    "project_dir", "compose_file", "env_file",
})


@dataclass(frozen=True, slots=True)
class Config:
    """Top-level configuration, mirrors stormpulse.toml structure."""

    agent: AgentConfig
    dashboard: DashboardConfig
    tls: TlsConfig
    auth: AuthConfig
    metrics: MetricsConfig
    project: ProjectConfig
    storage: StorageConfig
    commands: dict[str, CommandDef] = dataclasses.field(default_factory=dict)

    def validate_paths(self) -> None:
        """Check that all referenced file paths exist and are readable.

        Call after load_config() in production. Tests may skip this.
        Raises ConfigError listing all missing paths.
        """
        missing: list[str] = []
        for p in (
            self.tls.ca_cert, self.tls.client_cert, self.tls.client_key,
            self.auth.hmac_secret, self.project.compose_file,
        ):
            if not p.is_file():
                missing.append(str(p))
        if self.project.env_file and not self.project.env_file.is_file():
            missing.append(str(self.project.env_file))
        if not self.project.project_dir.is_dir():
            missing.append(f"{self.project.project_dir} (directory)")
        if not self.storage.db_path.parent.is_dir():
            missing.append(f"{self.storage.db_path.parent} (directory for db)")
        if missing:
            raise ConfigError(f"Missing files/directories: {', '.join(missing)}")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _require_section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    """Extract a required TOML section, raising ConfigError if missing."""
    if name not in raw:
        raise ConfigError(f"Missing required config section: [{name}]")
    section = raw[name]
    if not isinstance(section, dict):
        raise ConfigError(f"Config section [{name}] must be a table")
    return section


def _require_key(
    section: dict[str, Any],
    key: str,
    expected_type: type | tuple[type, ...],
    section_name: str,
) -> Any:
    """Extract a required key with type checking."""
    if key not in section:
        raise ConfigError(f"Missing required key '{key}' in [{section_name}]")
    value = section[key]
    if not isinstance(value, expected_type):
        if isinstance(expected_type, tuple):
            names = "/".join(t.__name__ for t in expected_type)
        else:
            names = expected_type.__name__
        raise ConfigError(
            f"Key '{key}' in [{section_name}] must be {names}, got {type(value).__name__}"
        )
    return value


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------


def _parse_agent(raw: dict[str, Any]) -> AgentConfig:
    s = _require_section(raw, "agent")
    disabled = s.get("disabled_commands", [])
    if not isinstance(disabled, list):
        raise ConfigError("'disabled_commands' in [agent] must be a list")
    for i, item in enumerate(disabled):
        if not isinstance(item, str):
            raise ConfigError(
                f"'disabled_commands[{i}]' in [agent] must be a string, "
                f"got {type(item).__name__}"
            )
    return AgentConfig(
        id=_require_key(s, "id", str, "agent"),
        pulse_token=_require_key(s, "pulse_token", str, "agent"),
        disabled_commands=frozenset(disabled),
    )


def _parse_dashboard(raw: dict[str, Any]) -> DashboardConfig:
    s = _require_section(raw, "dashboard")
    url = _require_key(s, "url", str, "dashboard")
    rmin = float(_require_key(s, "reconnect_min_seconds", (int, float), "dashboard"))
    rmax = float(_require_key(s, "reconnect_max_seconds", (int, float), "dashboard"))
    heartbeat = float(_require_key(s, "heartbeat_interval_seconds", (int, float), "dashboard"))
    if rmin <= 0 or rmax <= 0:
        raise ConfigError("Reconnect intervals must be positive")
    if heartbeat <= 0:
        raise ConfigError("heartbeat_interval_seconds must be positive")
    if rmin > rmax:
        raise ConfigError("reconnect_min_seconds must be <= reconnect_max_seconds")
    return DashboardConfig(
        url=url,
        reconnect_min_seconds=rmin,
        reconnect_max_seconds=rmax,
        heartbeat_interval_seconds=heartbeat,
    )


def _parse_tls(raw: dict[str, Any]) -> TlsConfig:
    s = _require_section(raw, "tls")
    return TlsConfig(
        ca_cert=Path(_require_key(s, "ca_cert", str, "tls")),
        client_cert=Path(_require_key(s, "client_cert", str, "tls")),
        client_key=Path(_require_key(s, "client_key", str, "tls")),
    )


def _parse_auth(raw: dict[str, Any]) -> AuthConfig:
    s = _require_section(raw, "auth")
    max_age = _require_key(s, "command_max_age_seconds", (int, float), "auth")
    if max_age <= 0:
        raise ConfigError("command_max_age_seconds must be positive")
    return AuthConfig(
        hmac_secret=Path(_require_key(s, "hmac_secret", str, "auth")),
        command_max_age_seconds=int(max_age),
    )


def _parse_metrics(raw: dict[str, Any]) -> MetricsConfig:
    s = _require_section(raw, "metrics")
    interval = float(_require_key(s, "push_interval_seconds", (int, float), "metrics"))
    if interval <= 0:
        raise ConfigError("push_interval_seconds must be positive")
    return MetricsConfig(
        push_interval_seconds=interval,
        collect_containers=_require_key(s, "collect_containers", bool, "metrics"),
    )


def _parse_project(raw: dict[str, Any]) -> ProjectConfig:
    s = _require_section(raw, "project")
    env_file_raw = s.get("env_file")
    if env_file_raw is not None and not isinstance(env_file_raw, str):
        raise ConfigError("Key 'env_file' in [project] must be a string")
    return ProjectConfig(
        project_dir=Path(_require_key(s, "project_dir", str, "project")),
        compose_file=Path(_require_key(s, "compose_file", str, "project")),
        docker_service_name=_require_key(s, "docker_service_name", str, "project"),
        env_file=Path(env_file_raw) if env_file_raw is not None else None,
    )


def _parse_storage(raw: dict[str, Any]) -> StorageConfig:
    s = _require_section(raw, "storage")
    return StorageConfig(
        db_path=Path(_require_key(s, "db_path", str, "storage")),
    )


def _parse_commands(raw: dict[str, Any]) -> dict[str, CommandDef]:
    """Parse optional [commands.*] sub-tables into CommandDef instances.

    Returns an empty dict if no [commands] section exists.
    Raises ConfigError for invalid command definitions.
    """
    section = raw.get("commands")
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ConfigError("[commands] must be a table")

    result: dict[str, CommandDef] = {}
    for name, entry in section.items():
        label = f"commands.{name}"
        if not isinstance(entry, dict):
            raise ConfigError(f"[{label}] must be a table")

        group = _require_key(entry, "group", str, label)
        if not group:
            raise ConfigError(f"'group' in [{label}] must not be empty")

        command = _require_key(entry, "command", list, label)
        if not command:
            raise ConfigError(f"'command' in [{label}] must be a non-empty list")
        for i, arg in enumerate(command):
            if not isinstance(arg, str):
                raise ConfigError(
                    f"'command[{i}]' in [{label}] must be a string, got {type(arg).__name__}"
                )
        if not command[0].startswith("/"):
            raise ConfigError(
                f"'command[0]' in [{label}] must be an absolute path (starts with /), "
                f"got {command[0]!r}"
            )

        timeout = _require_key(entry, "timeout", int, label)
        if timeout <= 0:
            raise ConfigError(f"'timeout' in [{label}] must be positive, got {timeout}")

        requires_confirmation = entry.get("requires_confirmation", False)
        if not isinstance(requires_confirmation, bool):
            raise ConfigError(
                f"'requires_confirmation' in [{label}] must be bool, "
                f"got {type(requires_confirmation).__name__}"
            )

        description = entry.get("description", "")
        if not isinstance(description, str):
            raise ConfigError(
                f"'description' in [{label}] must be a string, "
                f"got {type(description).__name__}"
            )

        params_raw = entry.get("params", {})
        if not isinstance(params_raw, dict):
            raise ConfigError(f"'params' in [{label}] must be a table")
        param_defs: dict[str, ParamDef] = {}
        for pname, pentry in params_raw.items():
            plabel = f"{label}.params.{pname}"
            if not isinstance(pentry, dict):
                raise ConfigError(f"[{plabel}] must be a table")
            placeholder = _require_key(pentry, "placeholder", str, plabel)
            if placeholder != pname:
                raise ConfigError(
                    f"'placeholder' in [{plabel}] must match the table key "
                    f"{pname!r}, got {placeholder!r}"
                )
            if placeholder in PROTECTED_PLACEHOLDERS:
                raise ConfigError(
                    f"'placeholder' in [{plabel}] must not override a protected "
                    f"placeholder: {placeholder!r}"
                )
            default_raw = pentry.get("default")
            if default_raw is not None and not isinstance(default_raw, str):
                raise ConfigError(
                    f"'default' in [{plabel}] must be a string, "
                    f"got {type(default_raw).__name__}"
                )
            pattern = _require_key(pentry, "pattern", str, plabel)
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ConfigError(
                    f"'pattern' in [{plabel}] is not valid regex: {exc}"
                ) from exc
            pdescription = pentry.get("description", "")
            if not isinstance(pdescription, str):
                raise ConfigError(
                    f"'description' in [{plabel}] must be a string, "
                    f"got {type(pdescription).__name__}"
                )
            param_defs[placeholder] = ParamDef(
                placeholder=placeholder,
                default=default_raw,
                pattern=pattern,
                description=pdescription,
            )

        result[name] = CommandDef(
            group=group,
            command=command,
            timeout=timeout,
            requires_confirmation=requires_confirmation,
            description=description,
            params=param_defs,
        )

    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(path: Path) -> Config:
    """Load and validate configuration from a TOML file.

    Raises ConfigError if the file is missing, malformed, or incomplete.
    Does not check that referenced paths (certs, keys, dirs) exist on disk —
    call Config.validate_paths() separately for that.
    """
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")

    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {path}: {exc}") from exc

    return Config(
        agent=_parse_agent(raw),
        dashboard=_parse_dashboard(raw),
        tls=_parse_tls(raw),
        auth=_parse_auth(raw),
        metrics=_parse_metrics(raw),
        project=_parse_project(raw),
        storage=_parse_storage(raw),
        commands=_parse_commands(raw),
    )
