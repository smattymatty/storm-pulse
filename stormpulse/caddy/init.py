"""``stormpulse caddy init`` - detect Caddy and append [caddy] to stormpulse.toml.

After writing the section, runs ``verify_drop_in_imported`` against the chosen
paths. The agent's boot check uses the same function, so an ``import directive
missing`` warning here means the agent will refuse to start.
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path

from stormpulse.caddy.sync import verify_drop_in_imported
from stormpulse.init import InitError, prompt
from stormpulse.init.files import default_config_path
from stormpulse.init.host_native_logs import (
    offer_caddy_events_log_group,
    offer_caddy_log_group,
)
from stormpulse.init.mode import InstallMode, detect_mode
from stormpulse.init.prompts import prompt_confirm
from stormpulse.init.system import restart_or_hint

# Search paths in priority order:
#   /etc/caddy/Caddyfile      - apt-installed default
#   /opt/caddy/Caddyfile      - common manual-install convention
#   /opt/garage/Caddyfile     - Caddy-co-located-with-Garage layout,
#                               which Storm uses on regional VPSes where
#                               Caddy reverse-proxies to Garage on the
#                               same host.
_CADDY_MAIN_SEARCH_PATHS = [
    Path("/etc/caddy/Caddyfile"),
    Path("/opt/caddy/Caddyfile"),
    Path("/opt/garage/Caddyfile"),
]


def find_caddy_main(override: str | None = None) -> Path | None:
    """Find the main Caddyfile. Returns None if not found."""
    if override:
        p = Path(override)
        return p if p.is_file() else None
    for p in _CADDY_MAIN_SEARCH_PATHS:
        if p.is_file():
            return p
    return None


_CADDY_TOML_TEMPLATE = """\

[caddy]
enabled = true
admin_url = "{admin_url}"
main_caddyfile = "{main_caddyfile}"
drop_in_path = "{drop_in_path}"
"""


def has_caddy_section(config_path: Path) -> bool:
    """Check if the TOML file already has a [caddy] section."""
    try:
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
        return "caddy" in raw
    except (OSError, tomllib.TOMLDecodeError):
        return False


def remove_caddy_section(lines: list[str]) -> list[str]:
    """Remove [caddy] section from TOML lines (line-based).

    Finds the [caddy] header and removes everything until the next
    section header or EOF. Preserves all other content. Mirrors
    ``remove_garage_section``.
    """
    result: list[str] = []
    in_caddy = False

    for line in lines:
        stripped = line.strip()
        if stripped == "[caddy]":
            in_caddy = True
            # Drop a preceding blank line if present, so removal doesn't
            # leave stacked blank lines in the file.
            if result and result[-1].strip() == "":
                result.pop()
            continue

        if in_caddy:
            if re.match(r"^\[(?!caddy\])", stripped):
                in_caddy = False
                result.append(line)
            continue

        result.append(line)

    return result


def append_caddy_section(
    config_path: Path,
    *,
    admin_url: str,
    main_caddyfile: str,
    drop_in_path: str,
    force: bool = False,
) -> None:
    """Append [caddy] section to an existing stormpulse.toml.

    If force=True and a [caddy] section already exists, removes it
    before appending. Raises InitError on file errors or on an
    existing section without force.
    """
    if not config_path.is_file():
        raise InitError(f"Config file not found: {config_path}")

    if has_caddy_section(config_path):
        if not force:
            raise InitError(
                f"[caddy] section already exists in {config_path}. "
                f"Use --force to overwrite."
            )
        try:
            lines = config_path.read_text("utf-8").splitlines(keepends=True)
        except OSError as exc:
            raise InitError(f"Cannot read {config_path}: {exc}") from exc
        lines = remove_caddy_section(lines)
        try:
            config_path.write_text("".join(lines))
        except OSError as exc:
            raise InitError(f"Cannot write {config_path}: {exc}") from exc

    block = _CADDY_TOML_TEMPLATE.format(
        admin_url=admin_url,
        main_caddyfile=main_caddyfile,
        drop_in_path=drop_in_path,
    )

    try:
        with open(config_path, "a") as f:
            f.write(block)
    except OSError as exc:
        raise InitError(f"Cannot append to {config_path}: {exc}") from exc


_DEFAULT_ADMIN_URL = "http://localhost:2019"


def prompt_caddy_values(
    *,
    main_caddyfile: Path,
    admin_url: str = _DEFAULT_ADMIN_URL,
    drop_in_path: str | None = None,
) -> dict[str, str]:
    """Prompt user for Caddy config values with defaults.

    Default drop-in lives alongside the main Caddyfile in a conf.d/
    subdirectory. Convention works whether the main file lives under
    /etc/caddy or /opt/garage.
    """
    default_drop_in = drop_in_path or str(
        main_caddyfile.parent / "conf.d" / "buckets-custom-domains.caddy"
    )

    while True:
        url = prompt("Admin URL", default=admin_url)
        if url.startswith(("http://", "https://")):
            break
        print("  Must start with http:// or https://", file=sys.stderr)

    while True:
        drop_in = prompt("Drop-in file path", default=default_drop_in)
        if drop_in.startswith("/"):
            break
        print("  Must be an absolute path", file=sys.stderr)

    return {
        "admin_url": url,
        "main_caddyfile": str(main_caddyfile),
        "drop_in_path": drop_in,
    }


def run_caddy_init(
    config_path: Path,
    *,
    main_caddyfile_override: str | None = None,
    force: bool = False,
) -> None:
    """Public entry point for ``stormpulse caddy init``."""
    # Writability gate. Replaces the old root-only check: USER mode is
    # the default on hardened boxes (ADR core/003).
    if not os.access(config_path.parent, os.W_OK):
        suggested = default_config_path()
        raise InitError(
            f"Cannot write to {config_path}. "
            f"In USER mode the config should be at {suggested} "
            f"and owned by the operator. In SYSTEM mode, re-run with sudo."
        )

    main_caddyfile = find_caddy_main(main_caddyfile_override)
    if main_caddyfile is None:
        searched = ", ".join(str(p) for p in _CADDY_MAIN_SEARCH_PATHS)
        raise InitError(
            f"No Caddy installation detected.\n"
            f"Searched: {searched}\n"
            f"Use --main-caddyfile to specify the path."
        )

    print(
        f"\nCaddy installation detected at {main_caddyfile}\n",
        file=sys.stderr,
    )

    if has_caddy_section(config_path) and not force:
        raise InitError(
            f"[caddy] section already exists in {config_path}. "
            f"Use --force to overwrite."
        )

    values = prompt_caddy_values(main_caddyfile=main_caddyfile)

    # Same verifier the agent's boot check uses. Surfacing here means
    # the user finds out now that they need an import directive, not
    # when the agent crashes on first start.
    drop_in_path = Path(values["drop_in_path"])
    import_err = verify_drop_in_imported(
        Path(values["main_caddyfile"]),
        drop_in_path,
    )
    if import_err:
        print(
            f"\n  WARNING: {main_caddyfile} does not import {drop_in_path}.",
            file=sys.stderr,
        )
        print(
            f"  Add one of these lines to {main_caddyfile} and reload Caddy:",
            file=sys.stderr,
        )
        print(f"    import {drop_in_path}", file=sys.stderr)
        print(
            f"    import {drop_in_path.parent}/*.caddy",
            file=sys.stderr,
        )
        print(
            "  The agent will refuse to start until the import is in place.",
            file=sys.stderr,
        )
        if not prompt_confirm(
            "\nWrite [caddy] section anyway? (you'll need to fix the "
            "import before restarting the agent)",
            default_yes=False,
        ):
            print("Aborted.", file=sys.stderr)
            return
    else:
        if not prompt_confirm("\nEnable Caddy integration?"):
            print("Aborted.", file=sys.stderr)
            return

    append_caddy_section(
        config_path,
        admin_url=values["admin_url"],
        main_caddyfile=values["main_caddyfile"],
        drop_in_path=values["drop_in_path"],
        force=force,
    )
    print(f"\n  [caddy] section written to {config_path}", file=sys.stderr)

    offer_caddy_log_group(config_path)
    offer_caddy_events_log_group(config_path)

    mode = detect_mode()
    restart_cmd = (
        "systemctl --user restart stormpulse"
        if mode is InstallMode.USER
        else "sudo systemctl restart stormpulse"
    )

    # If the import directive is missing, don't offer restart - the
    # agent's boot check would crash. Surface the next step plainly
    # instead.
    if import_err:
        print(
            "\n  Don't restart yet - fix the import directive above first.\n"
            f"  Then:    {restart_cmd}",
            file=sys.stderr,
        )
        return

    # No-escalation posture (see stormpulse.init.system): SYSTEM mode
    # prints the hint and never shells out; USER mode runs the user
    # unit restart after explicit operator consent.
    if mode is InstallMode.SYSTEM:
        restart_or_hint(mode)
    elif prompt_confirm("Restart stormpulse now?"):
        restart_or_hint(mode)
    else:
        print(
            f"\n  Restart later with:\n    {restart_cmd}",
            file=sys.stderr,
        )
