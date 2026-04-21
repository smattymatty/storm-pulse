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
    sensitive_output: bool = False
    params: dict[str, ParamDef] = dataclasses.field(default_factory=dict)


PROTECTED_PLACEHOLDERS: frozenset[str] = frozenset({
    "project_dir", "compose_file", "env_file",
})


_LOG_PARSERS: frozenset[str] = frozenset({"garage_s3", "stormpulse", "caddy_json", "docker_raw"})
_LOG_SOURCE_TYPES: frozenset[str] = frozenset({"file", "docker", "docker_stream"})
_LOG_NAME_PATTERN = re.compile(r"[a-zA-Z0-9_-]{1,50}")


@dataclass(frozen=True, slots=True)
class LogGroupConfig:
    """A single [[log_groups]] entry — one tailed log source."""

    name: str
    enabled: bool
    source_type: str
    source_path: Path
    filter_contains: str
    parser: str
    ship_interval_seconds: float
    max_lines_per_batch: int
    retention_days: int
    container_name: str = ""
    docker_binary: str = "/usr/bin/docker"


@dataclass(frozen=True, slots=True)
class GarageConfig:
    """Optional [garage] section — Garage S3 node management."""

    enabled: bool
    container_name: str
    garage_binary: str
    docker_binary: str
    config_path: Path
    state_push_interval_seconds: float


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
    garage: GarageConfig | None = None
    log_groups: list[LogGroupConfig] = dataclasses.field(default_factory=list)

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
        if self.garage and self.garage.enabled:
            if not Path(self.garage.config_path).is_file():
                missing.append(str(self.garage.config_path))
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

        sensitive_output = entry.get("sensitive_output", False)
        if not isinstance(sensitive_output, bool):
            raise ConfigError(
                f"'sensitive_output' in [{label}] must be bool, "
                f"got {type(sensitive_output).__name__}"
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
            sensitive_output=sensitive_output,
            params=param_defs,
        )

    return result


def _parse_garage(raw: dict[str, Any]) -> GarageConfig | None:
    """Parse optional [garage] section. Returns None if absent."""
    section = raw.get("garage")
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ConfigError("[garage] must be a table")

    enabled = _require_key(section, "enabled", bool, "garage")
    container_name = _require_key(section, "container_name", str, "garage")
    if not container_name:
        raise ConfigError("'container_name' in [garage] must not be empty")
    garage_binary = _require_key(section, "garage_binary", str, "garage")
    if not garage_binary:
        raise ConfigError("'garage_binary' in [garage] must not be empty")
    docker_binary = _require_key(section, "docker_binary", str, "garage")
    if not docker_binary.startswith("/"):
        raise ConfigError(
            f"'docker_binary' in [garage] must be an absolute path, got {docker_binary!r}"
        )
    config_path = Path(_require_key(section, "config_path", str, "garage"))
    interval = float(
        _require_key(section, "state_push_interval_seconds", (int, float), "garage")
    )
    if interval <= 0:
        raise ConfigError("'state_push_interval_seconds' in [garage] must be positive")

    return GarageConfig(
        enabled=enabled,
        container_name=container_name,
        garage_binary=garage_binary,
        docker_binary=docker_binary,
        config_path=config_path,
        state_push_interval_seconds=interval,
    )


def _parse_log_groups(raw: dict[str, Any]) -> list[LogGroupConfig]:
    """Parse the optional [[log_groups]] array."""
    entries = raw.get("log_groups", [])
    if not isinstance(entries, list):
        raise ConfigError("'log_groups' must be an array of tables")

    result: list[LogGroupConfig] = []
    seen_names: set[str] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"log_groups[{i}] must be a table")

        ctx = f"log_groups[{i}]"
        name = _require_key(entry, "name", str, ctx)
        if not _LOG_NAME_PATTERN.fullmatch(name):
            raise ConfigError(
                f"'name' in {ctx} must be alphanumeric/underscore/hyphen, 1-50 chars, got {name!r}"
            )
        if name in seen_names:
            raise ConfigError(f"Duplicate log group name: {name!r}")
        seen_names.add(name)

        source_type = _require_key(entry, "source_type", str, ctx)
        if source_type not in _LOG_SOURCE_TYPES:
            raise ConfigError(
                f"'source_type' in {ctx} must be one of {sorted(_LOG_SOURCE_TYPES)}, "
                f"got {source_type!r}"
            )

        container_name = ""
        docker_binary = "/usr/bin/docker"
        source_path = ""
        if source_type == "file":
            source_path = _require_key(entry, "source_path", str, ctx)
            if not source_path.startswith("/"):
                raise ConfigError(
                    f"'source_path' in {ctx} must be an absolute path, got {source_path!r}"
                )
        else:  # docker, docker_stream
            container_name = _require_key(entry, "container_name", str, ctx)
            if not container_name.strip():
                raise ConfigError(
                    f"'container_name' in {ctx} must be non-empty for docker sources"
                )
            docker_binary_raw = entry.get("docker_binary", "/usr/bin/docker")
            if not isinstance(docker_binary_raw, str) or not docker_binary_raw.startswith("/"):
                raise ConfigError(
                    f"'docker_binary' in {ctx} must be an absolute path"
                )
            docker_binary = docker_binary_raw

        parser = _require_key(entry, "parser", str, ctx)
        if parser not in _LOG_PARSERS:
            raise ConfigError(
                f"'parser' in {ctx} must be one of {sorted(_LOG_PARSERS)}, got {parser!r}"
            )

        interval = float(
            _require_key(entry, "ship_interval_seconds", (int, float), ctx)
        )
        if interval < 5.0:
            raise ConfigError(
                f"'ship_interval_seconds' in {ctx} must be >= 5.0, got {interval}"
            )

        batch_max = _require_key(entry, "max_lines_per_batch", int, ctx)
        if not 1 <= batch_max <= 200:
            raise ConfigError(
                f"'max_lines_per_batch' in {ctx} must be 1-200, got {batch_max}"
            )

        retention = _require_key(entry, "retention_days", int, ctx)
        if not 1 <= retention <= 365:
            raise ConfigError(
                f"'retention_days' in {ctx} must be 1-365, got {retention}"
            )

        filter_contains = entry.get("filter_contains", "")
        if not isinstance(filter_contains, str):
            raise ConfigError(f"'filter_contains' in {ctx} must be a string")

        result.append(LogGroupConfig(
            name=name,
            enabled=_require_key(entry, "enabled", bool, ctx),
            source_type=source_type,
            source_path=Path(source_path) if source_path else Path(""),
            filter_contains=filter_contains,
            parser=parser,
            ship_interval_seconds=interval,
            max_lines_per_batch=batch_max,
            retention_days=retention,
            container_name=container_name,
            docker_binary=docker_binary,
        ))
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
        garage=_parse_garage(raw),
        log_groups=_parse_log_groups(raw),
    )
