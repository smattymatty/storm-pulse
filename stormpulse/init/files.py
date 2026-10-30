"""Atomic file writing helpers for ``stormpulse init``."""

from __future__ import annotations

import os
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
        raise InitError(f"Permission denied writing {path}.") from exc
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise InitError(f"Failed to write {path}: {exc}") from exc


# Install paths (ADR CORE-003). Resolved lazily because $HOME can
# differ between import time and call time (e.g. tests).


def _config_home() -> Path:
    """``$XDG_CONFIG_HOME``, else ``~/.config``: the one base every config path uses."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return Path(xdg) if xdg else Path.home() / ".config"


def user_config_dir() -> Path:
    return _config_home() / "stormpulse"


def user_data_dir() -> Path:
    return Path.home() / ".local" / "share" / "stormpulse"


def user_config_path() -> Path:
    return user_config_dir() / "stormpulse.toml"


def user_systemd_path() -> Path:
    return _config_home() / "systemd" / "user" / "stormpulse.service"


def default_config_path() -> str:
    """Return ``$XDG_CONFIG_HOME/stormpulse/stormpulse.toml``, defaulting to ``~/.config``.

    CLI subcommands resolve their default config path through this helper
    so ``signoff unseal``, ``caddy init`` and friends find the real file.
    """
    return str(user_config_path())


def require_writable_config(config_path: Path) -> None:
    """Refuse a feature init whose config dir the operator cannot write (ADR CORE-003)."""
    if not os.access(config_path.parent, os.W_OK):
        raise InitError(
            f"Cannot write to {config_path}. The config should be at "
            f"{default_config_path()} and owned by the operator."
        )


def write_user_config_file(
    path: Path,
    content: str,
    *,
    force: bool = False,
) -> None:
    """Write the TOML config.

    Mode 0o600 (owner read/write only) because the file holds the pulse
    token. No chown: the caller is the unprivileged user and owns the
    file by virtue of having written it. The parent directory is
    created with mode 0o700.
    """
    if path.is_file() and not force:
        raise InitError(f"{path} already exists. Use --force to overwrite.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_file(path, content.encode("utf-8"), 0o600)


def write_user_systemd_unit(
    path: Path,
    content: str,
    *,
    force: bool = False,
) -> None:
    """Write the user systemd unit file with mode 0o644.

    The parent directory may not exist yet on a fresh box. Create it
    with mode 0o755 (matches the default systemd user-unit dir layout).
    """
    if path.is_file() and not force:
        raise InitError(f"{path} already exists. Use --force to overwrite.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    write_file(path, content.encode("utf-8"), 0o644)
