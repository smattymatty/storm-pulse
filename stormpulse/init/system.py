"""System setup commands (git safe.directory, systemd reload, restart)."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from stormpulse.init.checks import InitError
from stormpulse.init.prompts import prompt_confirm

logger = logging.getLogger("stormpulse")


def run_cmd(args: list[str], *, description: str) -> bool:
    """Run a system command, printing status. Returns True on success."""
    print(f"  {description}...", file=sys.stderr)
    try:
        subprocess.run(args, check=True, capture_output=True)
        return True
    except FileNotFoundError:
        print(f"    Command not found: {args[0]}", file=sys.stderr)
        return False
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace").strip()
        print(f"    Failed: {stderr or exc}", file=sys.stderr)
        return False


def run_system_setup(project_dir: Path) -> None:
    """Mark ``project_dir`` as a user-scoped git safe.directory.

    The agent runs as the operator, who already owns ``project_dir`` and
    reaches rootless Docker through the per-user socket (ADR CORE-003).
    """
    run_cmd(
        [
            "/usr/bin/git",
            "config",
            "--global",
            "--add",
            "safe.directory",
            str(project_dir),
        ],
        description=f"Marking {project_dir} as git safe.directory (user)",
    )


def run_user_daemon_reload() -> None:
    """Reload the user systemd to pick up the new user unit file."""
    if not run_cmd(
        ["/usr/bin/systemctl", "--user", "daemon-reload"],
        description="Reloading user systemd",
    ):
        raise InitError("systemctl --user daemon-reload failed")


def check_linger_enabled() -> bool:
    """Return True if the current user has linger enabled.

    User systemd units only survive logout when ``loginctl
    enable-linger`` is set for the user. Without linger, the unit
    stops when the operator logs out -- which makes the agent useless.
    """
    import os

    user = os.environ.get("USER") or ""
    if not user:
        return False
    try:
        result = subprocess.run(
            ["/usr/bin/loginctl", "show-user", user, "--property=Linger"],
            capture_output=True,
            text=True,
            check=False,
        )
        return "Linger=yes" in result.stdout
    except (FileNotFoundError, OSError):
        return False


def restart_user_unit() -> int:
    """Run ``systemctl --user restart stormpulse``. Return its exit code.

    Errors are logged via ``logger.error`` so they surface in the same
    journal stream as the agent itself.
    """
    print("Restarting user unit: stormpulse...", file=sys.stderr)
    result = subprocess.run(["systemctl", "--user", "restart", "stormpulse"])
    if result.returncode != 0:
        logger.error(
            "systemctl --user restart stormpulse exited %d",
            result.returncode,
        )
        return result.returncode
    print(
        "Restarted. Tail with: stormpulse logs",
        file=sys.stderr,
    )
    return 0


def offer_restart() -> None:
    """Ask before restarting the unit; print the command when the answer is no."""
    if prompt_confirm("Restart stormpulse now?"):
        restart_user_unit()
    else:
        print(
            "\n  Restart later with:\n    systemctl --user restart stormpulse",
            file=sys.stderr,
        )
