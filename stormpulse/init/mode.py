"""Install-mode detection and validation.

See ADR CORE-003. Two install modes:

- ``SYSTEM``: the original install path. Agent runs as the
  ``stormpulse`` system user via ``/etc/systemd/system/stormpulse.service``.
  Requires the box to have a rootful Docker daemon (`docker` group exists,
  socket at ``/var/run/docker.sock``).
- ``USER``: rootless install. Agent runs as the operator's unprivileged
  user via ``~/.config/systemd/user/stormpulse.service``. Talks to a
  per-user rootless dockerd through ``$XDG_RUNTIME_DIR/docker.sock``.

The hardening playbook ``001-ubuntu-baseline`` leaves boxes in the
rootless posture, so user mode is the default for any box that's been
hardened.
"""

from __future__ import annotations

import enum
import os
from pathlib import Path


class InstallMode(enum.Enum):
    SYSTEM = "system"
    USER = "user"


class ModeError(Exception):
    """Raised when the requested mode is incompatible with the runtime
    environment (e.g. user mode requested while running as root)."""


def rootless_socket_path() -> Path | None:
    """Return the per-user rootless docker socket path, or None if not set.

    Standard location: ``$XDG_RUNTIME_DIR/docker.sock``. On systemd
    boxes this is typically ``/run/user/<UID>/docker.sock``.
    """
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        return None
    return Path(runtime_dir) / "docker.sock"


def detect_mode() -> InstallMode:
    """Return the install mode that matches the operator's EUID.

    Root → SYSTEM (legacy install path). Non-root → USER (rootless,
    the documented default on hardened boxes).

    Why EUID and not a docker.sock probe: the probe races the install
    flow. Operators install the agent before bringing up their project's
    rootless dockerd, so $XDG_RUNTIME_DIR/docker.sock often isn't there
    yet -- the probe would falsely return SYSTEM, and the EUID check
    below would then reject the install with "needs root". EUID is a
    deterministic signal of operator intent ("am I root?" → "do I want
    a system unit?") and matches what ``validate_mode_for_euid`` will
    accept, so detect + validate never disagree.
    """
    if os.geteuid() == 0:
        return InstallMode.SYSTEM
    return InstallMode.USER


def resolve_mode(forced: InstallMode | None) -> InstallMode:
    """Combine an explicit override with auto-detection.

    If ``forced`` is set, return it verbatim. Otherwise return the
    detected mode. Validation against EUID happens separately so the
    caller can format error messages with full context.
    """
    if forced is not None:
        return forced
    return detect_mode()


def validate_mode_for_euid(mode: InstallMode) -> None:
    """Raise ``ModeError`` if the chosen mode is incompatible with EUID.

    Why this exists: a user-mode install MUST run unprivileged so the
    user systemd unit ends up owned by the right user. A system-mode
    install MUST run as root to write ``/etc/stormpulse/`` and the
    system unit. Mismatches produce confusing failures later; catch
    them up front.
    """
    is_root = os.geteuid() == 0
    if mode is InstallMode.USER and is_root:
        raise ModeError(
            "Rerun without sudo for user mode. The user systemd unit "
            "must be owned by the unprivileged user that runs rootless "
            "docker. (If you actually want a system install, pass "
            "--system to force it.)"
        )
    if mode is InstallMode.SYSTEM and not is_root:
        raise ModeError(
            "System-mode install needs root. Rerun with sudo, or pass "
            "--user to install as a user systemd unit (recommended on "
            "hardened boxes with rootless docker)."
        )
