"""Garage init - detect Garage installation and append [garage] to stormpulse.toml."""

from __future__ import annotations

import glob
import os
import re
import sys
import tomllib
from pathlib import Path

from stormpulse.init import InitError, prompt
from stormpulse.init.files import default_config_path
from stormpulse.init.mode import InstallMode, detect_mode
from stormpulse.init.prompts import prompt_confirm
from stormpulse.init.registry import register_init_step
from stormpulse.init.system import restart_or_hint

_GARAGE_CONFIG_SEARCH_PATHS = [
    Path("/opt/garage/garage.toml"),
    Path("/etc/garage/garage.toml"),
    Path("./garage.toml"),
]

# Storm rootless convention: the 002-garage playbook places config under
# each admin user's home so the rootless daemon (running as that user)
# can sanely bind-mount it without sudo gymnastics. Glob across /home/*/
# so the wizard auto-detects on a Storm-shaped box without forcing
# --garage-config. Checked after the fixed paths above so existing
# installs at /opt/garage/ continue to win when both exist.
_GARAGE_CONFIG_GLOB_PATTERNS = [
    "/home/*/garage/etc/garage.toml",
]


def find_garage_config(override: str | None = None) -> Path | None:
    """Find the Garage config file. Returns None if not found."""
    if override:
        p = Path(override)
        return p if p.is_file() else None
    for p in _GARAGE_CONFIG_SEARCH_PATHS:
        if p.is_file():
            return p
    for pattern in _GARAGE_CONFIG_GLOB_PATTERNS:
        for hit in sorted(glob.glob(pattern)):
            p = Path(hit)
            if p.is_file():
                return p
    return None


def _find_compose_file(garage_dir: Path) -> Path | None:
    """Locate the Garage compose file relative to where garage.toml lives.

    Searches alongside the config first, then one level up. The Storm
    rootless convention is ``~/<name>/etc/garage.toml`` for config with
    the compose file at ``~/<name>/docker-compose.yml`` -- one directory
    above the config. Older installs keep both alongside each other, so
    the alongside-search still wins when both exist.

    Returns the first match or ``None``. OSError from ``is_file()`` (a
    parent dir we cannot stat into) degrades to "not found here" rather
    than aborting the wizard.
    """
    search_dirs = [garage_dir, garage_dir.parent]
    for d in search_dirs:
        for name in ("docker-compose.yml", "docker-compose.yaml"):
            candidate = d / name
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
    return None


def parse_garage_container_name(compose_path: Path) -> str:
    """Extract container_name for the Garage service from a compose file.

    Uses naive line-by-line parsing (same pattern as init.py's
    parse_service_names). Scans for image: dxflrs/garage, then looks
    for container_name within that service block.

    Returns 'garaged' if not found or parse fails.
    """
    try:
        lines = compose_path.read_text("utf-8").splitlines()
    except OSError:
        return "garaged"

    in_services = False
    in_garage_service = False
    indent_level = 0

    for line in lines:
        stripped = line.rstrip()
        if stripped == "" or stripped.startswith("#"):
            continue

        if not in_services:
            if re.match(r"^services:\s*$", stripped) or stripped == "services:":
                in_services = True
            continue

        # Detect top-level key (end of services block)
        if re.match(r"^\S", stripped):
            break

        # Service-level entry (2-space indent)
        if re.match(r"^  [a-zA-Z0-9_][\w-]*:\s*$", stripped):
            in_garage_service = False
            indent_level = 2
            continue

        # Inside a service - check for garage image
        content = stripped.lstrip()
        current_indent = len(stripped) - len(content)

        if current_indent > indent_level:
            m = re.match(r"image:\s*[\"']?(dxflrs/garage\b)", content)
            if m:
                in_garage_service = True

            if in_garage_service:
                m = re.match(r"container_name:\s*[\"']?([a-zA-Z0-9_-]+)", content)
                if m:
                    return m.group(1)

    return "garaged"


_GARAGE_TOML_TEMPLATE = """\

[garage]
enabled = true
container_name = "{container_name}"
garage_binary = "{garage_binary}"
docker_binary = "{docker_binary}"
config_path = "{config_path}"
"""

# Appended only when garage.toml's [admin] block is detected. Powers the
# BUCKETS-006 quota write (UpdateBucket via the admin API). The token is a node
# secret, pointed at the same file Garage uses, never copied or sent over the wire.
_GARAGE_ADMIN_LINES = """\
admin_url = "{admin_url}"
admin_token_file = "{admin_token_file}"
"""


def _resolve_host_token_path(garage_config_path: Path, token_file_raw: str) -> str:
    """Find a *host-readable* path to the admin token.

    garage.toml's ``admin_token_file`` is the path inside Garage's container
    (bind-mounted), which the host agent usually cannot read. Try the raw value
    first (host-native installs), then the Storm rootless convention of a
    ``secrets/`` dir alongside garage.toml on the host. Returns "" if neither is
    a readable file, so init never emits a path that crash-loops the agent.
    """
    if not token_file_raw:
        return ""
    candidates = [
        Path(token_file_raw),
        garage_config_path.parent / "secrets" / Path(token_file_raw).name,
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.R_OK):
            return str(candidate)
    return ""


def discover_admin_api(garage_config_path: Path) -> tuple[str, str]:
    """Read garage.toml's ``[admin]`` block, return ``(admin_url, admin_token_file)``.

    Derives the loopback admin URL from ``api_bind_addr`` (a ``0.0.0.0`` / ``::``
    bind becomes ``127.0.0.1`` since the agent connects from the same host) and
    resolves a *host-readable* token path (garage.toml's value is container-
    relative, so it is re-rooted against the host secrets dir and verified to
    exist). Either element is "" when not found: ``("", "")`` if the admin API
    isn't enabled, ``(url, "")`` if it is but no readable token file was located
    (the operator then wires the token by hand rather than init writing a path
    that doesn't exist on the host).
    """
    try:
        with open(garage_config_path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return "", ""
    admin = raw.get("admin")
    if not isinstance(admin, dict):
        return "", ""
    bind = admin.get("api_bind_addr", "")
    token_file = admin.get("admin_token_file", "")
    if not (isinstance(bind, str) and bind):
        return "", ""
    host, _, port = bind.rpartition(":")
    if not port.isdigit():
        return "", ""
    host = host.strip("[]")
    if host in ("", "0.0.0.0", "::", "*"):
        host = "127.0.0.1"
    admin_url = f"http://{host}:{port}"
    host_token = _resolve_host_token_path(
        garage_config_path, token_file if isinstance(token_file, str) else "",
    )
    return admin_url, host_token


def has_garage_section(config_path: Path) -> bool:
    """Check if the TOML file already has a [garage] section."""
    try:
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
        return "garage" in raw
    except (OSError, tomllib.TOMLDecodeError):
        return False


def remove_garage_section(lines: list[str]) -> list[str]:
    """Remove [garage] section from TOML lines (line-based).

    Finds the [garage] header and removes everything until the next
    section header or EOF. Preserves all other content.
    """
    result: list[str] = []
    in_garage = False

    for line in lines:
        stripped = line.strip()
        if stripped == "[garage]":
            in_garage = True
            # Also remove a preceding blank line if it exists
            if result and result[-1].strip() == "":
                result.pop()
            continue

        if in_garage:
            # Check if we hit the next section
            if re.match(r"^\[(?!garage\])", stripped):
                in_garage = False
                result.append(line)
            # Otherwise skip this line (still in garage section)
            continue

        result.append(line)

    return result


def append_garage_section(
    config_path: Path,
    *,
    container_name: str,
    garage_binary: str,
    docker_binary: str,
    garage_config_path: str,
    admin_url: str = "",
    admin_token_file: str = "",
    force: bool = False,
) -> None:
    """Append [garage] section to an existing stormpulse.toml.

    If force=True and a [garage] section already exists, removes
    it first before appending the new one. When ``admin_url`` +
    ``admin_token_file`` are given (auto-discovered from garage.toml), the
    admin-API keys are emitted too.

    Raises InitError on file errors.
    """
    if not config_path.is_file():
        raise InitError(f"Config file not found: {config_path}")

    if has_garage_section(config_path):
        if not force:
            raise InitError(
                f"[garage] section already exists in {config_path}. "
                f"Use --force to overwrite."
            )
        # Remove existing section
        try:
            lines = config_path.read_text("utf-8").splitlines(keepends=True)
        except OSError as exc:
            raise InitError(f"Cannot read {config_path}: {exc}") from exc
        lines = remove_garage_section(lines)
        try:
            config_path.write_text("".join(lines))
        except OSError as exc:
            raise InitError(f"Cannot write {config_path}: {exc}") from exc

    block = _GARAGE_TOML_TEMPLATE.format(
        container_name=container_name,
        garage_binary=garage_binary,
        docker_binary=docker_binary,
        config_path=garage_config_path,
    )
    if admin_url and admin_token_file:
        block += _GARAGE_ADMIN_LINES.format(
            admin_url=admin_url, admin_token_file=admin_token_file,
        )

    try:
        with open(config_path, "a") as f:
            f.write(block)
    except OSError as exc:
        raise InitError(f"Cannot append to {config_path}: {exc}") from exc


def prompt_garage_values(
    *,
    container_name: str = "garaged",
    garage_binary: str = "/garage",
    docker_binary: str = "/usr/bin/docker",
    garage_config_path: str = "/opt/garage/garage.toml",
) -> dict[str, str]:
    """Prompt user for Garage config values with defaults."""
    container_name = prompt("Container name", default=container_name)
    garage_binary = prompt("Garage binary", default=garage_binary)
    docker_binary = prompt("Docker binary", default=docker_binary)

    return {
        "container_name": container_name,
        "garage_binary": garage_binary,
        "docker_binary": docker_binary,
        "garage_config_path": garage_config_path,
    }


def run_garage_init(
    config_path: Path,
    *,
    garage_config_override: str | None = None,
    force: bool = False,
) -> None:
    """Public entry point for ``stormpulse garage init``."""
    # Writability gate. Replaces the old root-only check: USER mode is
    # the default on hardened boxes (ADR core/003), so requiring sudo
    # was wrong. The CLI has already routed ``config_path`` through
    # ``default_config_path()`` for the operator's mode, so we just
    # verify the parent dir is writable for whoever's running us.
    if not os.access(config_path.parent, os.W_OK):
        suggested = default_config_path()
        raise InitError(
            f"Cannot write to {config_path}. "
            f"In USER mode the config should be at {suggested} "
            f"and owned by the operator. In SYSTEM mode, re-run with sudo."
        )

    # Find garage config
    garage_config = find_garage_config(garage_config_override)
    if garage_config is None:
        searched_fixed = ", ".join(str(p) for p in _GARAGE_CONFIG_SEARCH_PATHS)
        searched_glob = ", ".join(_GARAGE_CONFIG_GLOB_PATTERNS)
        raise InitError(
            f"No Garage installation detected.\n"
            f"Searched: {searched_fixed}\n"
            f"Globbed: {searched_glob}\n"
            f"Use --garage-config to specify the path."
        )

    print(f"\nGarage installation detected at {garage_config}\n", file=sys.stderr)

    # Check for existing section
    if has_garage_section(config_path) and not force:
        raise InitError(
            f"[garage] section already exists in {config_path}. "
            f"Use --force to overwrite."
        )

    # Detect container name from compose file. Searches alongside
    # garage.toml first, then one directory up (Storm etc/ layout).
    container_name = "garaged"
    compose_path = _find_compose_file(garage_config.parent)
    if compose_path is not None:
        container_name = parse_garage_container_name(compose_path)
        if container_name != "garaged":
            print(
                f"  Container name: {container_name}  (from {compose_path})",
                file=sys.stderr,
            )
    else:
        print(
            "  No docker-compose.yml found alongside Garage config "
            "or in its parent. Using defaults.",
            file=sys.stderr,
        )

    # Auto-discover the admin HTTP API from garage.toml's [admin] block, so the
    # quota write (UpdateBucket) works without the operator hand-editing config.
    admin_url, admin_token_file = discover_admin_api(garage_config)
    if admin_url and admin_token_file:
        print(
            f"  Admin API: {admin_url}  (token: {admin_token_file})",
            file=sys.stderr,
        )
    elif admin_url:
        print(
            "  Garage admin API detected, but no host-readable admin token file "
            "was found (garage.toml's path is container-internal). Add [garage] "
            "admin_token_file (or admin_token) by hand; quota writes need it.",
            file=sys.stderr,
        )
    else:
        print(
            "  Garage admin API not detected in garage.toml; quota writes will "
            "need [garage] admin_url + admin_token added by hand.",
            file=sys.stderr,
        )

    # Prompt for values
    values = prompt_garage_values(
        container_name=container_name,
        garage_config_path=str(garage_config),
    )

    # Confirm
    if not prompt_confirm("\nEnable Garage integration?"):
        print("Aborted.", file=sys.stderr)
        return

    # Write
    append_garage_section(
        config_path,
        container_name=str(values["container_name"]),
        garage_binary=str(values["garage_binary"]),
        docker_binary=str(values["docker_binary"]),
        garage_config_path=str(values["garage_config_path"]),
        admin_url=admin_url,
        admin_token_file=admin_token_file,
        force=force,
    )
    print(f"\n  [garage] section written to {config_path}", file=sys.stderr)

    # Restart. No-escalation posture (see stormpulse.init.system):
    # SYSTEM mode prints the hint and never shells out; USER mode runs
    # the user unit restart after explicit operator consent.
    mode = detect_mode()
    if mode is InstallMode.SYSTEM:
        restart_or_hint(mode)
    elif prompt_confirm("Restart stormpulse now?"):
        restart_or_hint(mode)
    else:
        print(
            "\n  Restart later with:\n    systemctl --user restart stormpulse",
            file=sys.stderr,
        )


def garage_init_step(config_path: Path) -> None:
    """Init step: detect Garage, prompt, append a [garage] section.

    Registered with the init orchestrator (see stormpulse.init.registry).
    ``run_init`` calls this after the base config file is written.
    """
    print("\nChecking for Garage installation...", file=sys.stderr)
    garage_config = find_garage_config()
    if garage_config is None:
        print("  No Garage installation found. Skipping.", file=sys.stderr)
        return

    print(f"  Found: {garage_config}", file=sys.stderr)
    if not prompt_confirm("\nEnable Garage integration?"):
        return

    # Detect container name from the compose file (alongside or parent).
    container = "garaged"
    cp = _find_compose_file(garage_config.parent)
    if cp is not None:
        container = parse_garage_container_name(cp)

    values = prompt_garage_values(
        container_name=container,
        garage_config_path=str(garage_config),
    )
    append_garage_section(
        config_path,
        container_name=str(values["container_name"]),
        garage_binary=str(values["garage_binary"]),
        docker_binary=str(values["docker_binary"]),
        garage_config_path=str(values["garage_config_path"]),
    )


register_init_step(garage_init_step)
