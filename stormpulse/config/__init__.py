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

from stormpulse.config.param_schema import ParamSchema, schema_declaration_error

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

    Validated by ``pattern`` (regex for short identifiers), ``max_bytes`` (size
    cap for opaque blobs like shell text), or ``schema`` (a JSON blob's declared
    shape, checked after decoding; needs ``max_bytes`` so the blob is capped
    before it is parsed). Unvalidated params fail at construction.
    """

    placeholder: str
    default: str | None
    pattern: str | None = None
    description: str = ""
    max_bytes: int | None = None
    # A secret input (an S3 secret key): the value reaches the handler but is
    # redacted from the wide-event context at dispatch, never a durable record.
    secret: bool = False
    schema: ParamSchema | None = None

    def __post_init__(self) -> None:
        if self.pattern is None and self.max_bytes is None:
            raise ValueError(
                f"ParamDef {self.placeholder!r}: must set pattern or max_bytes "
                f"(unvalidated params are a footgun)"
            )
        if self.schema is not None:
            if self.pattern is not None or self.max_bytes is None:
                raise ValueError(
                    f"ParamDef {self.placeholder!r}: schema needs max_bytes and "
                    f"excludes pattern (the blob is capped, then decoded, then shaped)"
                )
            err = schema_declaration_error(self.schema)
            if err is not None:
                raise ValueError(f"ParamDef {self.placeholder!r}: {err}")
        # A new sink for command data must never meet an untagged credential:
        # a credential-shaped name without secret=True fails at construction.
        if not self.secret and re.search(
            r"secret|password|token|passphrase", self.placeholder, re.IGNORECASE
        ):
            raise ValueError(
                f"ParamDef {self.placeholder!r}: credential-shaped name requires "
                f"secret=True (redacts it from event and log context)"
            )


# Execution-mode discriminator the dispatcher routes on. Subprocess vs job vs
# agent-internal refresh used to be smeared across a magic command name, a bool
# and "fell through to subprocess"; now it is one field, validated at construction.
CommandMode = Literal["subprocess", "job", "refresh"]

# A "job" command's lazy handler thunk: validated runtime params -> JobHandler,
# or None when unservable on this host. Typed loosely: the concrete types live in
# Framework (commands/jobs.py), which Foundation must not import (CORE-000).
CommandHandler = Callable[[dict[str, str]], Any]


@dataclass(frozen=True, slots=True)
class CommandSpec:
    """A single whitelisted command: its schema and, for a job, its handler.
    One spec per command is the registry's whole source of truth; there is no
    parallel name->factory map to drift. ``mode`` is the execution discriminator:
    ``subprocess`` runs ``command`` as argv with ``shell=False`` (absolute binary
    first, no handler); ``job`` hands a long-running command to the JobManager
    through the lazy ``handler`` thunk, ``command`` being the sentinel ``[name]``;
    ``refresh`` is the agent-owned collect-and-push, synthesized for any
    Integration declaring ``collect_state``. Illegal mixes fail at construction.
    """

    group: str
    command: list[str]
    timeout: int
    mode: CommandMode = "subprocess"
    requires_confirmation: bool = False
    description: str = ""
    sensitive_output: bool = False
    read_only: bool = (
        False  # no state mutation; skips the garage post-success refresh hook
    )
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
    {"garage_s3", "stormpulse", "caddy_json", "docker_raw", "django", "journald"}
)
_LOG_SOURCE_TYPES: frozenset[str] = frozenset(
    {"file", "docker", "docker_stream", "journald"}
)
_LOG_NAME_PATTERN = re.compile(r"[a-zA-Z0-9_-]{1,50}")
_DEFAULT_DOCKER_BINARY = "/usr/bin/docker"


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
    container_name: str = ""
    docker_binary: str = _DEFAULT_DOCKER_BINARY
    unit: str = ""


# Top-level TOML tables Foundation knows by name. Every other table is an
# Integration's raw config section, parsed by its own module (CORE-005 decision
# 4: Foundation stops naming Integrations). ``log_groups`` is an array, excluded.
_SUBJECT_PATTERN = re.compile(r"[a-zA-Z0-9_.-]{1,64}")

# Ceilings for the deploy probe's filesystem walk (CORE-009 decision 4): a node
# may narrow them, never widen them. An unbounded walk of $HOME reads the
# neighbourhood of every secret on the box to find one binary; the ADR refuses.
_DEPLOY_MAX_DEPTH_CEILING = 8
_DEPLOY_MAX_BYTES_CEILING = 1_048_576
_DEPLOY_DEFAULT_DEPTH = 3
_DEPLOY_DEFAULT_BYTES = 65_536


@dataclass(frozen=True, slots=True)
class DeployProbeConfig:
    """One subject the `deploy` investigation can answer for on this node.

    Every parameter the probe uses resolves from here and nowhere else (CORE-009
    decision 3): nothing about what to look for crosses the wire, so the control
    plane cannot point this node at a path of its choosing. ``expected_root`` is
    where the unit installs the thing; ``search_roots`` are the bounded places
    the probe may look. A binary found in a search root but outside the expected
    root is the finding, not an error (decision 5): the 2026-09-07 residue shape.
    """

    subject: str
    units: tuple[str, ...]
    expected_root: Path
    search_roots: tuple[Path, ...]
    ports: tuple[int, ...] = ()
    max_depth: int = _DEPLOY_DEFAULT_DEPTH
    max_bytes: int = _DEPLOY_DEFAULT_BYTES


_CORE_SECTIONS: frozenset[str] = frozenset(
    {
        "agent",
        "dashboard",
        "tls",
        "auth",
        "metrics",
        "project",
        "storage",
        "commands",
        "investigate",
    }
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
    deploy_probes: dict[str, DeployProbeConfig] = dataclasses.field(
        default_factory=dict,
    )
    # The same tables, unparsed: precedence (CORE-009 D10) is field-by-field and
    # a parsed DeployProbeConfig cannot tell an operator value from a filled-in
    # default. Merging happens before validation, on the whole.
    deploy_probe_tables: dict[str, Any] = dataclasses.field(default_factory=dict)

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
    str: "string",
    int: "int",
    float: "float",
    bool: "bool",
    list: "list",
    dict: "table",
}


def _check_type(
    value: Any,
    key: str,
    expected_type: type | tuple[type, ...],
    section_name: str,
) -> Any:
    """Type-check a present value; reject bool for numeric keys (bool is an int subclass)."""
    types = expected_type if isinstance(expected_type, tuple) else (expected_type,)
    if not isinstance(value, expected_type) or (
        isinstance(value, bool) and bool not in types
    ):
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
    return {name: _parse_one_command(name, entry) for name, entry in section.items()}


def _parse_one_command(name: str, entry: Any) -> CommandSpec:
    """Validate one ``[commands.<name>]`` table; raise ConfigError on any problem."""
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

    requires_confirmation = optional_key(
        entry, "requires_confirmation", bool, False, label
    )
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
        pdef = _parse_one_param(pname, pentry, label)
        param_defs[pdef.placeholder] = pdef

    return CommandSpec(
        group=group,
        command=command,
        timeout=timeout,
        requires_confirmation=requires_confirmation,
        description=description,
        sensitive_output=sensitive_output,
        params=param_defs,
    )


def _parse_one_param(pname: str, pentry: Any, label: str) -> ParamDef:
    """Validate one ``[commands.<name>.params.<pname>]`` table into a ParamDef."""
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
        raise ConfigError(f"'pattern' in [{plabel}] is not valid regex: {exc}") from exc
    pdescription = optional_key(pentry, "description", str, "", plabel)
    psecret = optional_key(pentry, "secret", bool, False, plabel)
    try:
        return ParamDef(
            placeholder=placeholder,
            default=default_raw,
            pattern=pattern,
            description=pdescription,
            secret=psecret,
        )
    except ValueError as exc:
        raise ConfigError(f"[{plabel}]: {exc}") from exc


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


def _parse_deploy_probes(raw: dict[str, Any]) -> dict[str, DeployProbeConfig]:
    """Parse the optional ``[investigate.deploy.<subject>]`` tables.

    Soft, per the ``[[log_groups]]`` precedent: an invalid subject is skipped
    with a loud warning, never aborting boot, because this feeds a diagnostic,
    not the agent's ability to run. A skipped subject is not silent either: the
    investigation reports INCONCLUSIVE naming the section it lacks, never
    CLEARED (CORE-009 decision 3). Only a structurally-wrong container
    ([investigate] or [investigate.deploy] not a table) is fatal.
    """
    investigate = raw.get("investigate", {})
    if not isinstance(investigate, dict):
        raise ConfigError("'investigate' must be a table")
    subjects = investigate.get("deploy", {})
    if not isinstance(subjects, dict):
        raise ConfigError("'investigate.deploy' must be a table of subjects")

    out: dict[str, DeployProbeConfig] = {}
    for subject, entry in subjects.items():
        try:
            out[subject] = _parse_one_deploy_probe(subject, entry)
        except ConfigError as exc:
            logger.warning(
                "Skipping invalid [investigate.deploy.%s]: %s. `stormpulse "
                "investigate deploy` reports INCONCLUSIVE for this subject "
                "until it is fixed.",
                subject,
                exc,
            )
    return out


def _deploy_probe_tables(raw: dict[str, Any]) -> dict[str, Any]:
    """The node's own `[investigate.deploy.*]` tables, unvalidated.

    Structural refusals only: a non-table container is the operator's intent
    being unreadable, which is fatal. Per-subject validity is decided after the
    merge, because a table that is invalid alone may be a legal override of a
    contributed default.
    """
    investigate = raw.get("investigate", {})
    if not isinstance(investigate, dict):
        raise ConfigError("'investigate' must be a table")
    subjects = investigate.get("deploy", {})
    if not isinstance(subjects, dict):
        raise ConfigError("'investigate.deploy' must be a table of subjects")
    return {k: v for k, v in subjects.items() if isinstance(v, dict)}


def _subject_as_table(subject: Any) -> dict[str, Any]:
    """An SdkDeploySubject flattened into the shape its config table has.

    One vocabulary for both sources: the descriptor's field names ARE the
    table's key names, so the overlay below is a plain dict update and there is
    no translation layer to drift.
    """
    table: dict[str, Any] = {
        "units": list(subject.units),
        "expected_root": subject.expected_root,
        "search_roots": list(subject.search_roots),
        "ports": list(subject.ports),
    }
    if subject.max_depth is not None:
        table["max_depth"] = subject.max_depth
    if subject.max_bytes is not None:
        table["max_bytes"] = subject.max_bytes
    return table


def merge_deploy_probes(
    tables: dict[str, Any],
    contributed: tuple[Any, ...] = (),
) -> dict[str, DeployProbeConfig]:
    """Contributed subjects overlaid by the node's own tables (CORE-009 D10).

    Precedence points one way: a descriptor supplies a default, the operator's
    table overrides it key by key, and `enabled = false` on either side removes
    the subject. A package can widen nothing the operator has narrowed, which is
    why contributed subjects may exist at all. Merging before validation is
    deliberate: a node table carrying only `search_roots` is not a valid subject
    by itself but a legal narrowing of one; validated alone it would be refused.
    """
    merged: dict[str, dict[str, Any]] = {}
    for subject in contributed:
        merged[subject.subject] = _subject_as_table(subject)
    for name, table in tables.items():
        merged.setdefault(name, {}).update(table)

    out: dict[str, DeployProbeConfig] = {}
    for name, table in merged.items():
        if table.get("enabled") is False:
            continue
        try:
            out[name] = _parse_one_deploy_probe(
                name,
                {k: v for k, v in table.items() if k != "enabled"},
            )
        except ConfigError as exc:
            logger.warning(
                "Skipping invalid [investigate.deploy.%s]: %s. `stormpulse "
                "investigate deploy` reports INCONCLUSIVE for this subject "
                "until it is fixed.",
                name,
                exc,
            )
    return out


def _parse_one_deploy_probe(subject: str, entry: Any) -> DeployProbeConfig:
    """Validate one subject table; raise ConfigError on any problem."""
    ctx = f"investigate.deploy.{subject}"
    if not isinstance(entry, dict):
        raise ConfigError(f"[{ctx}] must be a table")
    if not _SUBJECT_PATTERN.fullmatch(subject):
        raise ConfigError(
            f"subject name must be alphanumeric/underscore/hyphen/dot, "
            f"1-64 chars, got {subject!r}"
        )

    units = _require_str_list(entry, "units", ctx)
    for unit in units:
        if "/" in unit:
            raise ConfigError(
                f"'units' in [{ctx}] holds systemd unit names, not paths; got {unit!r}"
            )

    expected_root = _require_abs_path(entry, "expected_root", ctx)
    search_roots = tuple(
        _abs_path(value, "search_roots", ctx)
        for value in _require_str_list(entry, "search_roots", ctx)
    )
    # An expected_root outside every search root means the probe cannot look
    # where the unit installs, so every verdict it reaches is about somewhere
    # else. Refused at load rather than reported as an absence on the box.
    if not any(_is_within(expected_root, root) for root in search_roots):
        raise ConfigError(
            f"'expected_root' {str(expected_root)!r} in [{ctx}] is not inside "
            f"any of 'search_roots'; the probe could never look at it"
        )

    ports = tuple(
        _check_port(value, ctx) for value in optional_key(entry, "ports", list, [], ctx)
    )
    max_depth = _bounded_int(
        entry,
        "max_depth",
        ctx,
        _DEPLOY_DEFAULT_DEPTH,
        1,
        _DEPLOY_MAX_DEPTH_CEILING,
    )
    max_bytes = _bounded_int(
        entry,
        "max_bytes",
        ctx,
        _DEPLOY_DEFAULT_BYTES,
        1024,
        _DEPLOY_MAX_BYTES_CEILING,
    )
    return DeployProbeConfig(
        subject=subject,
        units=tuple(units),
        expected_root=expected_root,
        search_roots=search_roots,
        ports=ports,
        max_depth=max_depth,
        max_bytes=max_bytes,
    )


def _require_str_list(entry: dict[str, Any], key: str, ctx: str) -> list[str]:
    values = require_key(entry, key, list, ctx)
    if not values:
        raise ConfigError(f"'{key}' in [{ctx}] must not be empty")
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"'{key}' in [{ctx}] must be a list of non-empty strings, got {value!r}"
            )
    return [str(value) for value in values]


def _abs_path(value: str, key: str, ctx: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ConfigError(f"'{key}' in [{ctx}] must be absolute, got {value!r}")
    # A traversal segment makes a declared bound unreadable: "/home/storm/.."
    # is / wearing a costume, and the walk's containment check would honour it.
    if ".." in path.parts:
        raise ConfigError(f"'{key}' in [{ctx}] must not contain '..', got {value!r}")
    return path


def _require_abs_path(entry: dict[str, Any], key: str, ctx: str) -> Path:
    return _abs_path(require_key(entry, key, str, ctx), key, ctx)


def _is_within(candidate: Path, root: Path) -> bool:
    """True when ``candidate`` is ``root`` or sits underneath it."""
    return candidate == root or root in candidate.parents


def _check_port(value: Any, ctx: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(f"'ports' in [{ctx}] must be integers, got {value!r}")
    if not 1 <= value <= 65535:
        raise ConfigError(f"'ports' in [{ctx}] must be 1-65535, got {value}")
    return value


def _bounded_int(
    entry: dict[str, Any],
    key: str,
    ctx: str,
    default: int,
    low: int,
    high: int,
) -> int:
    value = optional_key(entry, key, int, default, ctx)
    if isinstance(value, bool) or not low <= value <= high:
        raise ConfigError(f"'{key}' in [{ctx}] must be {low}-{high}, got {value!r}")
    return int(value)


def _parse_log_groups(raw: dict[str, Any]) -> list[LogGroupConfig]:
    """Parse the optional [[log_groups]] array.

    A malformed *individual* entry is SKIPPED with a loud, actionable warning.
    Log shipping is the least-critical loop, and one bad block must never take
    metrics, the Headroom quota loop and liveness down with it on a systemd
    restart loop (the 2026-06-06 `path`-vs-`source_path` incident). The valid
    groups still load; fix the bad block and restart. Only a structurally-wrong
    top-level value (``log_groups`` not an array) is fatal.
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
                i,
                exc,
            )
            continue
        seen_names.add(group.name)
        result.append(group)
    return result


def _parse_one_log_group(
    i: int,
    entry: Any,
    seen_names: set[str],
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
    docker_binary = _DEFAULT_DOCKER_BINARY
    source_path = ""
    unit = ""
    if source_type == "file":
        source_path = require_key(entry, "source_path", str, ctx)
        if not source_path.startswith("/"):
            raise ConfigError(
                f"'source_path' in {ctx} must be an absolute path, got {source_path!r}"
            )
    elif source_type == "journald":
        # A systemd-supervised service logs to the journal, not to a file it
        # owns. Tailing the journal means no log file to rotate, no directory
        # to create, and no permissions to get right on the unit's behalf.
        unit = require_key(entry, "unit", str, ctx)
        if not unit.strip():
            raise ConfigError(f"'unit' in {ctx} must be non-empty for journald sources")
        if any(c.isspace() for c in unit):
            raise ConfigError(
                f"'unit' in {ctx} must not contain whitespace, got {unit!r}"
            )
    else:  # docker, docker_stream
        container_name = require_key(entry, "container_name", str, ctx)
        if not container_name.strip():
            raise ConfigError(
                f"'container_name' in {ctx} must be non-empty for docker sources"
            )
        docker_binary = optional_key(
            entry, "docker_binary", str, _DEFAULT_DOCKER_BINARY, ctx
        )
        if not docker_binary.startswith("/"):
            raise ConfigError(f"'docker_binary' in {ctx} must be an absolute path")

    parser = require_key(entry, "parser", str, ctx)
    if parser not in _LOG_PARSERS:
        raise ConfigError(
            f"'parser' in {ctx} must be one of {sorted(_LOG_PARSERS)}, got {parser!r}"
        )

    interval = float(require_key(entry, "ship_interval_seconds", (int, float), ctx))
    # Floor is 2s so the activity feed keeps pace with the 2s metrics/state push.
    # max_lines_per_batch caps each ship, so a tighter interval flushes more often
    # without enlarging a batch; the `logging init` wizard still defaults slower.
    if interval < 2.0:
        raise ConfigError(
            f"'ship_interval_seconds' in {ctx} must be >= 2.0, got {interval}"
        )

    batch_max = require_key(entry, "max_lines_per_batch", int, ctx)
    if not 1 <= batch_max <= 200:
        raise ConfigError(
            f"'max_lines_per_batch' in {ctx} must be 1-200, got {batch_max}"
        )

    # Dead-knob removal: the agent tails and ships, it stores nothing, so it
    # never enforced retention. A stale key from an older config warns, not fails.
    if "retention_days" in entry:
        logger.warning(
            "'retention_days' in %s is deprecated and ignored; remove it "
            "(the agent stores no logs to retain)",
            ctx,
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
        container_name=container_name,
        docker_binary=docker_binary,
        unit=unit,
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
        deploy_probes=_parse_deploy_probes(raw),
        deploy_probe_tables=_deploy_probe_tables(raw),
    )
