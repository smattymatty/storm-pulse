"""Atomic file writing helpers for ``stormpulse init``."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from stormpulse.init.checks import InitError


def write_file(path: Path, data: bytes, mode: int) -> None:
    """Write data atomically with correct permissions."""
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_bytes(data)
        os.chmod(tmp, mode)
        tmp.rename(path)
    except PermissionError as exc:
        tmp.unlink(missing_ok=True)
        raise InitError(
            f"Permission denied writing {path}. Run with sudo."
        ) from exc
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise InitError(f"Failed to write {path}: {exc}") from exc


CONFIG_PATH = Path("/etc/stormpulse/stormpulse.toml")
SYSTEMD_PATH = Path("/etc/systemd/system/stormpulse.service")

# User-mode paths. Used when ``stormpulse init`` ran in user mode (see
# ADR CORE-003 and stormpulse.init.mode). Resolved lazily because $HOME
# can differ between import time and call time (e.g. tests, sudo -E).

def user_config_dir() -> Path:
    return Path.home() / ".config" / "stormpulse"


def user_data_dir() -> Path:
    return Path.home() / ".local" / "share" / "stormpulse"


def user_config_path() -> Path:
    return user_config_dir() / "stormpulse.toml"


def user_systemd_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "stormpulse.service"


def default_config_path() -> str:
    """Pick the ``stormpulse.toml`` path matching the current install mode.

    Root (EUID 0) → ``/etc/stormpulse/stormpulse.toml`` (legacy system
    install). Non-root → ``$XDG_CONFIG_HOME/stormpulse/stormpulse.toml``,
    defaulting to ``~/.config/stormpulse/stormpulse.toml``.

    Mirrors the EUID-based mode detection in ``stormpulse.init.mode``
    and the ``--creds-dir`` default logic in ``stormpulse.cli``. CLI
    subcommands MUST resolve their default config path through this
    helper rather than hardcoding the system path: rootless is the
    norm on hardened Storm boxes, and a hardcoded ``/etc/stormpulse``
    default silently misses the real file (``signoff unseal`` cannot
    open the seal, ``caddy init`` cannot find the config to edit,
    etc.).
    """
    if os.geteuid() == 0:
        return str(CONFIG_PATH)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return str(base / "stormpulse" / "stormpulse.toml")


def write_config_file(path: Path, content: str, *, force: bool = False) -> None:
    """Write the TOML config with mode 0o640, owned by root:stormpulse."""
    if path.is_file() and not force:
        raise InitError(
            f"{path} already exists. Use --force to overwrite."
        )
    write_file(path, content.encode("utf-8"), 0o640)
    try:
        shutil.chown(path, "root", "stormpulse")
    except (LookupError, PermissionError):
        pass  # stormpulse user/group may not exist yet in test environments


def write_user_config_file(
    path: Path, content: str, *, force: bool = False,
) -> None:
    """Write the TOML config for a user-mode install.

    Mode 0o600 (owner read/write only) because the file holds the pulse
    token. No chown: the caller is the unprivileged user and owns the
    file by virtue of having written it. The parent directory is
    created with mode 0o700.
    """
    if path.is_file() and not force:
        raise InitError(
            f"{path} already exists. Use --force to overwrite."
        )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_file(path, content.encode("utf-8"), 0o600)


def write_systemd_unit(
    path: Path, content: str, *, force: bool = False,
) -> None:
    """Write the systemd unit file with mode 0o644."""
    if path.is_file() and not force:
        raise InitError(
            f"{path} already exists. Use --force to overwrite."
        )
    write_file(path, content.encode("utf-8"), 0o644)


def write_user_systemd_unit(
    path: Path, content: str, *, force: bool = False,
) -> None:
    """Write the user-mode systemd unit file.

    Same 0o644 as the system unit, but the parent directory may not
    exist yet on a fresh box. Create it with mode 0o755 (matches the
    default systemd user-unit dir layout).
    """
    if path.is_file() and not force:
        raise InitError(
            f"{path} already exists. Use --force to overwrite."
        )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    write_file(path, content.encode("utf-8"), 0o644)
