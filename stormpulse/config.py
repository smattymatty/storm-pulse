"""TOML config loading and validation."""

from __future__ import annotations

import dataclasses
import logging
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Raised when configuration is missing, invalid, or incomplete."""


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
    """Declares an overridable placeholder for a command.

    Either ``pattern`` (regex for short identifiers) or ``max_bytes`` (size
    cap for opaque content blobs like a Caddyfile fragment) must be set;
    both can be set if a value must match both. Unvalidated params are
    rejected at construction time to prevent footguns.
    """

    placeholder: str
    default: str | None
    pattern: str | None = None
    description: str = ""
    max_bytes: int | None = None
    # A secret input (an S3 secret key): the value reaches the handler but is
    # redacted from the wide-event context at dispatch, never a durable record.
    secret: bool = False

    def __post_init__(self) -> None:
        if self.pattern is None and self.max_bytes is None:
            raise ValueError(
                f"ParamDef {self.placeholder!r}: must set pattern or max_bytes "
                f"(unvalidated params are a footgun)"
            )
        # A new sink for command data must never meet an untagged credential:
        # a credential-shaped name without secret=True fails at construction.
        if not self.secret and re.search(
            r"secret|password|token|passphrase", self.placeholder, re.IGNORECASE
        ):
            raise ValueError(
                f"ParamDef {self.placeholder!r}: credential-shaped name requires "
                f"secret=True (redacts it from event and log context)"
            )


# Execution-mode discriminator the agent dispatcher routes on. The whole point
# of carrying it explicitly: subprocess vs long-running-job vs agent-internal
# refresh used to be smeared across a magic command name, a bool, and "fell
# through to subprocess". Now it is one field, validated at construction.
CommandMode = Literal["subprocess", "job", "refresh"]

# A "job" command's lazy handler thunk: given validated runtime params, build
# the JobHandler (or None when unservable on this host). Typed loosely here
# because the concrete JobHandler / LongRunningFactory live in the Framework
# layer (commands/jobs.py), which Foundation (config) must not import per the
# CORE-000 four-layer topology. The real type re-forms in commands/ and agent/.
CommandHandler = Callable[[dict[str, str]], Any]


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """A single whitelisted command: its schema and, for a job, its handler.

    One spec per command is the registry's whole source of truth - there is no
    parallel name->factory map to fall out of sync with, because there is no
    second map. ``mode`` is the execution discriminator:

    - ``subprocess``: ``command`` is a real argv run with ``shell=False``; the
      first element must be an absolute binary path. No handler.
    - ``job``: a long-running command handed to the JobManager. ``handler`` is a
      lazy per-integration thunk; ``command`` is the sentinel ``[name]`` that
      backs the advertised wire template.
    - ``refresh``: an agent-owned "collect this integration's state now and push
      metrics" command, synthesized for any Integration declaring
      ``collect_state``. No handler (the agent owns the one generic routine).

    Illegal combinations are rejected at construction, so half-registration is
    structurally impossible rather than caught by a hand-maintained test.
    """

    group: str
    command: list[str]
    timeout: int
    mode: CommandMode = "subprocess"
    requires_confirmation: bool = False
    description: str = ""
    sensitive_output: bool = False
    read_only: bool = False  # no state mutation; skips the garage post-success refresh hook
    # mutates, but is dispatched repeatedly by a reconciliation loop, so no single
    # success is the "did it land" moment a push would serve; also skips the hook
    # (the periodic walk reflects it each cycle). Sibling of read_only.
    self_reconciling: bool = False
    handler: CommandHandler | None = None
    params: dict[str, ParamDef] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mode == "job":
            if self.handler is None:
                raise ValueError(
                    f"CommandSpec {self.command!r}: mode 'job' requires a handler "
                    "(a job with no handler is the half-registration footgun this "
                    "guard exists to make impossible)"
                )
        elif self.handler is not None:
            raise ValueError(
                f"CommandSpec {self.command!r}: mode {self.mode!r} must not carry a "
                "handler (only 'job' commands have one)"
            )
        if self.mode == "subprocess" and (
            not self.command or not self.command[0].startswith("/")
        ):
            raise ValueError(
                f"CommandSpec {self.command!r}: mode 'subprocess' requires an "
                "absolute binary path as command[0] (the Layer-4 whitelist invariant)"
            )

    @property
    def long_running(self) -> bool:
        """Derived: a job rides the JobManager path. Kept for the wire manifest and dispatch readers."""
        return self.mode == "job"


PROTECTED_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "project_dir",
        "compose_file",
        "env_file",
    }
)


_LOG_PARSERS: frozenset[str] = frozenset(
    {"garage_s3", "stormpulse", "caddy_json", "docker_raw", "django"}
)
_LOG_SOURCE_TYPES: frozenset[str] = frozenset({"file", "docker", "docker_stream"})
_LOG_NAME_PATTERN = re.compile(r"[a-zA-Z0-9_-]{1,50}")


@dataclass(frozen=True, slots=True)
class LogGroupConfig:
    """A single [[log_groups]] entry - one tailed log source."""

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


# Top-level TOML tables Foundation knows by name. Everything else that is a
# table is an Integration's raw config section, keyed by id and parsed by that
# Integration's own module (CORE-005 decision 4: Foundation stops naming
# Integrations). ``log_groups`` is an array, not a table, so it is excluded.
_CORE_SECTIONS: frozenset[str] = frozenset(
    {"agent", "dashboard", "tls", "auth", "metrics", "project", "storage", "commands"}
)


@dataclass(frozen=True, slots=True)
class Config:
    """Top-level configuration, mirrors stormpulse.toml structure.

    ``integrations`` holds the raw TOML tables for any non-core section, keyed
    by id. Foundation does not type them: each Integration parses its own
    section at bootstrap via the registry (CORE-005 decision 4).
    """

    agent: AgentConfig
    dashboard: DashboardConfig
    tls: TlsConfig
    auth: AuthConfig
    metrics: MetricsConfig
    project: ProjectConfig
    storage: StorageConfig
    commands: dict[str, CommandSpec] = dataclasses.field(default_factory=dict)
    integrations: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    log_groups: list[LogGroupConfig] = dataclasses.field(default_factory=list)

    def validate_paths(self) -> None:
        """Check that all referenced core file paths exist and are readable.

        Call after load_config() in production. Tests may skip this. Raises
        ConfigError listing all missing paths. Integration paths are NOT
        checked here: a missing Integration path soft-disables that one
        Integration at bootstrap, it does not abort the core agent (CORE-005
        decision 5). The fatal/soft line is core-fatal, integration-soft.
        """
        missing: list[str] = []
        for p in (
            self.tls.ca_cert,
            self.tls.client_cert,
            self.tls.client_key,
            self.auth.hmac_secret,
            self.project.compose_file,
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


def require_section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    """Extract a required TOML section, raising ConfigError if missing."""
    if name not in raw:
        raise ConfigError(f"Missing required config section: [{name}]")
    section = raw[name]
    if not isinstance(section, dict):
        raise ConfigError(f"Config section [{name}] must be a table")
    return section


_TYPE_NAMES: dict[type, str] = {
    str: "string", int: "int", float: "float", bool: "bool", list: "list", dict: "table",
}


def _check_type(
    value: Any, key: str, expected_type: type | tuple[type, ...], section_name: str,
) -> Any:
    """Type-check a present value; reject bool for numeric keys (bool is an int subclass)."""
    types = expected_type if isinstance(expected_type, tuple) else (expected_type,)
    if not isinstance(value, expected_type) or (isinstance(value, bool) and bool not in types):
        names = "/".join(_TYPE_NAMES.get(t, t.__name__) for t in types)
        raise ConfigError(
            f"Key '{key}' in [{section_name}] must be {names}, got {type(value).__name__}"
        )
    return value


def require_key(
    section: dict[str, Any],
    key: str,
    expected_type: type | tuple[type, ...],
    section_name: str,
) -> Any:
    """Extract a required key with type checking."""
    if key not in section:
        raise ConfigError(f"Missing required key '{key}' in [{section_name}]")
    return _check_type(section[key], key, expected_type, section_name)


def optional_key(
    section: dict[str, Any],
    key: str,
    expected_type: type | tuple[type, ...],
    default: Any,
    section_name: str,
) -> Any:
    """Extract an optional key with type checking; return default if absent."""
    if key not in section:
        return default
    return _check_type(section[key], key, expected_type, section_name)


def _parse_agent(raw: dict[str, Any]) -> AgentConfig:
    s = require_section(raw, "agent")
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
        id=require_key(s, "id", str, "agent"),
        pulse_token=require_key(s, "pulse_token", str, "agent"),
        disabled_commands=frozenset(disabled),
    )


def _parse_dashboard(raw: dict[str, Any]) -> DashboardConfig:
    s = require_section(raw, "dashboard")
    url = require_key(s, "url", str, "dashboard")
    rmin = float(require_key(s, "reconnect_min_seconds", (int, float), "dashboard"))
    rmax = float(require_key(s, "reconnect_max_seconds", (int, float), "dashboard"))
    heartbeat = float(
        require_key(s, "heartbeat_interval_seconds", (int, float), "dashboard")
    )
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
    s = require_section(raw, "tls")
    return TlsConfig(
        ca_cert=Path(require_key(s, "ca_cert", str, "tls")),
        client_cert=Path(require_key(s, "client_cert", str, "tls")),
        client_key=Path(require_key(s, "client_key", str, "tls")),
    )


def _parse_auth(raw: dict[str, Any]) -> AuthConfig:
    s = require_section(raw, "auth")
    max_age = require_key(s, "command_max_age_seconds", (int, float), "auth")
    if max_age <= 0:
        raise ConfigError("command_max_age_seconds must be positive")
    return AuthConfig(
        hmac_secret=Path(require_key(s, "hmac_secret", str, "auth")),
        command_max_age_seconds=int(max_age),
    )


def _parse_metrics(raw: dict[str, Any]) -> MetricsConfig:
    s = require_section(raw, "metrics")
    interval = float(require_key(s, "push_interval_seconds", (int, float), "metrics"))
    if interval <= 0:
        raise ConfigError("push_interval_seconds must be positive")
    return MetricsConfig(
        push_interval_seconds=interval,
        collect_containers=require_key(s, "collect_containers", bool, "metrics"),
    )


def _parse_project(raw: dict[str, Any]) -> ProjectConfig:
    s = require_section(raw, "project")
    env_file_raw = optional_key(s, "env_file", str, None, "project")
    return ProjectConfig(
        project_dir=Path(require_key(s, "project_dir", str, "project")),
        compose_file=Path(require_key(s, "compose_file", str, "project")),
        docker_service_name=require_key(s, "docker_service_name", str, "project"),
        env_file=Path(env_file_raw) if env_file_raw is not None else None,
    )


def _parse_storage(raw: dict[str, Any]) -> StorageConfig:
    s = require_section(raw, "storage")
    return StorageConfig(
        db_path=Path(require_key(s, "db_path", str, "storage")),
    )


def _parse_commands(raw: dict[str, Any]) -> dict[str, CommandSpec]:
    """Parse optional [commands.*] sub-tables into CommandSpec instances.

    Returns an empty dict if no [commands] section exists.
    Raises ConfigError for invalid command definitions.
    """
    section = raw.get("commands")
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ConfigError("[commands] must be a table")

    result: dict[str, CommandSpec] = {}
    for name, entry in section.items():
        label = f"commands.{name}"
        if not isinstance(entry, dict):
            raise ConfigError(f"[{label}] must be a table")

        group = require_key(entry, "group", str, label)
        if not group:
            raise ConfigError(f"'group' in [{label}] must not be empty")

        command = require_key(entry, "command", list, label)
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

        timeout = require_key(entry, "timeout", int, label)
        if timeout <= 0:
            raise ConfigError(f"'timeout' in [{label}] must be positive, got {timeout}")

        requires_confirmation = optional_key(entry, "requires_confirmation", bool, False, label)
        sensitive_output = optional_key(entry, "sensitive_output", bool, False, label)
        if optional_key(entry, "long_running", bool, False, label):
            raise ConfigError(
                f"[{label}]: 'long_running' is not supported for config-defined "
                "commands. Long-running (job) commands are contributed by "
                "integrations, which supply the handler; a config command is "
                "always a subprocess. Remove the key."
            )
        description = optional_key(entry, "description", str, "", label)

        params_raw = entry.get("params", {})
        if not isinstance(params_raw, dict):
            raise ConfigError(f"'params' in [{label}] must be a table")
        param_defs: dict[str, ParamDef] = {}
        for pname, pentry in params_raw.items():
            plabel = f"{label}.params.{pname}"
            if not isinstance(pentry, dict):
                raise ConfigError(f"[{plabel}] must be a table")
            placeholder = require_key(pentry, "placeholder", str, plabel)
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
            default_raw = optional_key(pentry, "default", str, None, plabel)
            pattern = require_key(pentry, "pattern", str, plabel)
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ConfigError(
                    f"'pattern' in [{plabel}] is not valid regex: {exc}"
                ) from exc
            pdescription = optional_key(pentry, "description", str, "", plabel)
            psecret = optional_key(pentry, "secret", bool, False, plabel)
            try:
                param_defs[placeholder] = ParamDef(
                    placeholder=placeholder,
                    default=default_raw,
                    pattern=pattern,
                    description=pdescription,
                    secret=psecret,
                )
            except ValueError as exc:
                raise ConfigError(f"[{plabel}]: {exc}") from exc

        result[name] = CommandSpec(
            group=group,
            command=command,
            timeout=timeout,
            requires_confirmation=requires_confirmation,
            description=description,
            sensitive_output=sensitive_output,
            params=param_defs,
        )

    return result


def _parse_integrations(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Capture every non-core top-level table as a raw Integration section.

    Foundation does not know which ids are Integrations and does not parse them
    (CORE-005 decision 4): it returns the raw tables keyed by id, and each
    Integration's own module parses its section at bootstrap via the registry.
    A section no registered Integration claims is simply never read.
    """
    out: dict[str, dict[str, Any]] = {}
    for key, value in raw.items():
        if key in _CORE_SECTIONS or key == "log_groups":
            continue
        if isinstance(value, dict):
            out[key] = value
    return out


def _parse_log_groups(raw: dict[str, Any]) -> list[LogGroupConfig]:
    """Parse the optional [[log_groups]] array.

    A malformed *individual* entry is SKIPPED with a loud, actionable warning
    rather than raising. Log shipping is the least-critical loop, and a single
    bad ``[[log_groups]]`` block must never crash the whole agent, which would
    take metrics, the Headroom quota loop, and liveness down with it on a systemd
    restart loop (the 2026-06-06 `path`-vs-`source_path` incident). The valid
    groups still load; fix the bad block and restart to enable it. Only a
    structurally-wrong top-level value (``log_groups`` not an array) is fatal.
    """
    entries = raw.get("log_groups", [])
    if not isinstance(entries, list):
        raise ConfigError("'log_groups' must be an array of tables")

    result: list[LogGroupConfig] = []
    seen_names: set[str] = set()
    for i, entry in enumerate(entries):
        try:
            group = _parse_one_log_group(i, entry, seen_names)
        except ConfigError as exc:
            logger.warning(
                "Skipping invalid log group at index %d: %s. The agent runs "
                "without it; fix this [[log_groups]] block and restart to enable.",
                i, exc,
            )
            continue
        seen_names.add(group.name)
        result.append(group)
    return result


def _parse_one_log_group(
    i: int, entry: Any, seen_names: set[str],
) -> LogGroupConfig:
    """Validate one ``[[log_groups]]`` entry; raise ConfigError on any problem.

    Does not mutate ``seen_names``: the caller records the name only after a
    successful parse, so a skipped (invalid) entry never blocks a later valid
    group from reusing the same name.
    """
    if not isinstance(entry, dict):
        raise ConfigError(f"log_groups[{i}] must be a table")

    ctx = f"log_groups[{i}]"
    name = require_key(entry, "name", str, ctx)
    if not _LOG_NAME_PATTERN.fullmatch(name):
        raise ConfigError(
            f"'name' in {ctx} must be alphanumeric/underscore/hyphen, 1-50 chars, got {name!r}"
        )
    if name in seen_names:
        raise ConfigError(f"Duplicate log group name: {name!r}")

    source_type = require_key(entry, "source_type", str, ctx)
    if source_type not in _LOG_SOURCE_TYPES:
        raise ConfigError(
            f"'source_type' in {ctx} must be one of {sorted(_LOG_SOURCE_TYPES)}, "
            f"got {source_type!r}"
        )

    container_name = ""
    docker_binary = "/usr/bin/docker"
    source_path = ""
    if source_type == "file":
        source_path = require_key(entry, "source_path", str, ctx)
        if not source_path.startswith("/"):
            raise ConfigError(
                f"'source_path' in {ctx} must be an absolute path, got {source_path!r}"
            )
    else:  # docker, docker_stream
        container_name = require_key(entry, "container_name", str, ctx)
        if not container_name.strip():
            raise ConfigError(
                f"'container_name' in {ctx} must be non-empty for docker sources"
            )
        docker_binary = optional_key(entry, "docker_binary", str, "/usr/bin/docker", ctx)
        if not docker_binary.startswith("/"):
            raise ConfigError(f"'docker_binary' in {ctx} must be an absolute path")

    parser = require_key(entry, "parser", str, ctx)
    if parser not in _LOG_PARSERS:
        raise ConfigError(
            f"'parser' in {ctx} must be one of {sorted(_LOG_PARSERS)}, got {parser!r}"
        )

    interval = float(
        require_key(entry, "ship_interval_seconds", (int, float), ctx)
    )
    # Floor is 2s so the activity feed can keep pace with the 2s metrics/state
    # push and feel real-time alongside the storage bars. Logs are a heavier
    # stream than a metric snapshot (one line per S3 request), but each ship is
    # capped by max_lines_per_batch, so a tighter interval just flushes more
    # often, it does not enlarge a batch. The `logging init` wizard still
    # DEFAULTS to a slower interval; this only permits going tighter on purpose.
    if interval < 2.0:
        raise ConfigError(
            f"'ship_interval_seconds' in {ctx} must be >= 2.0, got {interval}"
        )

    batch_max = require_key(entry, "max_lines_per_batch", int, ctx)
    if not 1 <= batch_max <= 200:
        raise ConfigError(
            f"'max_lines_per_batch' in {ctx} must be 1-200, got {batch_max}"
        )

    retention = require_key(entry, "retention_days", int, ctx)
    if not 1 <= retention <= 365:
        raise ConfigError(
            f"'retention_days' in {ctx} must be 1-365, got {retention}"
        )

    filter_contains = optional_key(entry, "filter_contains", str, "", ctx)

    return LogGroupConfig(
        name=name,
        enabled=require_key(entry, "enabled", bool, ctx),
        source_type=source_type,
        source_path=Path(source_path) if source_path else Path(""),
        filter_contains=filter_contains,
        parser=parser,
        ship_interval_seconds=interval,
        max_lines_per_batch=batch_max,
        retention_days=retention,
        container_name=container_name,
        docker_binary=docker_binary,
    )


def load_config(path: Path) -> Config:
    """Load and validate configuration from a TOML file.

    Raises ConfigError if the file is missing, malformed, or incomplete.
    Does not check that referenced paths (certs, keys, dirs) exist on disk -
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
        integrations=_parse_integrations(raw),
        log_groups=_parse_log_groups(raw),
    )
