"""Atomic file writing helpers for ``stormpulse init``."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from stormpulse.init.checks import InitError


def _write_file(path: Path, data: bytes, mode: int) -> None:
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


_CONFIG_PATH = Path("/etc/stormpulse/stormpulse.toml")
_SYSTEMD_PATH = Path("/etc/systemd/system/stormpulse.service")


def write_config_file(path: Path, content: str, *, force: bool = False) -> None:
    """Write the TOML config with mode 0o640, owned by root:stormpulse."""
    if path.is_file() and not force:
        raise InitError(
            f"{path} already exists. Use --force to overwrite."
        )
    _write_file(path, content.encode("utf-8"), 0o640)
    try:
        shutil.chown(path, "root", "stormpulse")
    except (LookupError, PermissionError):
        pass  # stormpulse user/group may not exist yet in test environments


def write_systemd_unit(
    path: Path, content: str, *, force: bool = False,
) -> None:
    """Write the systemd unit file with mode 0o644."""
    if path.is_file() and not force:
        raise InitError(
            f"{path} already exists. Use --force to overwrite."
        )
    _write_file(path, content.encode("utf-8"), 0o644)
