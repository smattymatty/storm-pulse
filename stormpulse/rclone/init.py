"""``stormpulse rclone init`` - detect rclone and append [rclone] to
stormpulse.toml, so a Runner box is configured without hand-editing TOML.

Runs the same ``rclone version`` check the agent's precondition uses, so a
missing or broken binary is caught here, not at first agent start."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from stormpulse.init import InitError
from stormpulse.init.files import default_config_path
from stormpulse.init.mode import InstallMode, detect_mode
from stormpulse.init.prompts import prompt, prompt_confirm
from stormpulse.init.system import restart_or_hint

_DEFAULT_BINARY = "/usr/bin/rclone"
_VERSION_TIMEOUT_SECONDS = 15

_RCLONE_TOML_TEMPLATE = """\

[rclone]
enabled = true
binary_path = "{binary_path}"
"""


def find_rclone_binary(override: str | None = None) -> str | None:
    """Absolute path to a working rclone, or None. Confirms it runs, not just
    that the file exists (the precondition's ``rclone version`` gate)."""
    candidate = override or _DEFAULT_BINARY
    if not Path(candidate).is_file():
        return None
    try:
        proc = subprocess.run(
            [candidate, "version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT_SECONDS,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return candidate if proc.returncode == 0 else None


def has_rclone_section(config_path: Path) -> bool:
    """Whether the TOML already has an [rclone] section."""
    try:
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
        return "rclone" in raw
    except (OSError, tomllib.TOMLDecodeError):
        return False


def remove_rclone_section(lines: list[str]) -> list[str]:
    """Drop the [rclone] section (line-based), preserving all else. Mirrors
    ``remove_caddy_section``."""
    result: list[str] = []
    in_rclone = False
    for line in lines:
        stripped = line.strip()
        if stripped == "[rclone]":
            in_rclone = True
            if result and result[-1].strip() == "":
                result.pop()
            continue
        if in_rclone:
            if re.match(r"^\[(?!rclone\])", stripped):
                in_rclone = False
                result.append(line)
            continue
        result.append(line)
    return result


def append_rclone_section(
    config_path: Path,
    *,
    binary_path: str,
    force: bool = False,
) -> None:
    """Append [rclone] to an existing stormpulse.toml. With force, replaces an
    existing section; without it, an existing section is an error."""
    if not config_path.is_file():
        raise InitError(f"Config file not found: {config_path}")
    if has_rclone_section(config_path):
        if not force:
            raise InitError(
                f"[rclone] section already exists in {config_path}. "
                f"Use --force to overwrite."
            )
        try:
            lines = config_path.read_text("utf-8").splitlines(keepends=True)
            config_path.write_text("".join(remove_rclone_section(lines)))
        except OSError as exc:
            raise InitError(f"Cannot rewrite {config_path}: {exc}") from exc
    block = _RCLONE_TOML_TEMPLATE.format(binary_path=binary_path)
    try:
        with open(config_path, "a") as f:
            f.write(block)
    except OSError as exc:
        raise InitError(f"Cannot append to {config_path}: {exc}") from exc


def run_rclone_init(
    config_path: Path,
    *,
    binary_path_override: str | None = None,
    force: bool = False,
) -> None:
    """Public entry point for ``stormpulse rclone init``."""
    if not os.access(config_path.parent, os.W_OK):
        suggested = default_config_path()
        raise InitError(
            f"Cannot write to {config_path}. "
            f"In USER mode the config should be at {suggested} "
            f"and owned by the operator. In SYSTEM mode, re-run with sudo."
        )

    binary_path = find_rclone_binary(binary_path_override)
    if binary_path is None:
        searched = binary_path_override or _DEFAULT_BINARY
        raise InitError(
            f"No working rclone found at {searched}.\n"
            f"Install rclone (https://rclone.org/install), or use "
            f"--binary-path to point at it."
        )

    if has_rclone_section(config_path) and not force:
        raise InitError(
            f"[rclone] section already exists in {config_path}. "
            f"Use --force to overwrite."
        )

    print(f"\nrclone detected at {binary_path}\n", file=sys.stderr)
    print(
        "  This configures the box as a backup Runner: it will accept and "
        "run\n  migration and backup jobs. Configure this only on a "
        "dedicated Runner,\n  not on a storage node.\n",
        file=sys.stderr,
    )
    if not prompt_confirm("Configure this box as a backup Runner?"):
        print("Aborted.", file=sys.stderr)
        return

    chosen = prompt("rclone binary path", default=binary_path)
    if not chosen.startswith("/"):
        raise InitError(f"Binary path must be absolute, got {chosen!r}")

    append_rclone_section(config_path, binary_path=chosen, force=force)
    print(f"\n  [rclone] section written to {config_path}", file=sys.stderr)

    mode = detect_mode()
    restart_cmd = (
        "systemctl --user restart stormpulse"
        if mode is InstallMode.USER
        else "sudo systemctl restart stormpulse"
    )
    if mode is InstallMode.SYSTEM:
        restart_or_hint(mode)
    elif prompt_confirm("Restart stormpulse now?"):
        restart_or_hint(mode)
    else:
        print(f"\n  Restart later with:\n    {restart_cmd}", file=sys.stderr)
