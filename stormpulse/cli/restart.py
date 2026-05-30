"""CLI handler for ``stormpulse restart``.

Restarts the systemd unit the agent runs under. Mode-aware: user
installs auto-restart their user unit; system installs print the
restart command rather than escalating to sudo, matching the
no-escalation posture of the install/init/update flow.

The restart helper is shared with ``stormpulse update``, which calls
it after a successful ``pipx install --force``. Standalone
``stormpulse restart`` exists so operators can recycle the agent
without redoing the install, which matters today as the workaround
for the seal-state-stuck dashboard banner (the dashboard learns
signoff state only at register, so a CLI seal/unseal does not
propagate until the agent reconnects). See ADR core/004 and the
in-flight ``signoff.state`` envelope work for the proper fix.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys

from stormpulse.init.mode import InstallMode, detect_mode

logger = logging.getLogger("stormpulse")


def _restart_user_unit() -> int:
    """Run ``systemctl --user restart stormpulse``. Return its exit code.

    Prints a status line before invoking and a success/tail line on
    return when the unit restarted cleanly. Errors are logged via
    ``logger.error`` so they surface in the same journal stream as
    the agent itself.
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
        "Restarted. Tail with: journalctl --user -u stormpulse -f",
        file=sys.stderr,
    )
    return 0


def _print_system_restart_hint() -> None:
    """Print the system-mode restart instruction without executing.

    Matches the no-escalation posture: the wrapper never shells out
    to sudo. The operator runs the command in a context that already
    holds the privilege.
    """
    print(
        "System install detected. Restart when ready: "
        "systemctl restart stormpulse",
        file=sys.stderr,
    )


def restart_or_hint(mode: InstallMode) -> int:
    """Restart the unit (user mode) or print the hint (system mode).

    Returns the exit code the caller should propagate. ``0`` when the
    system-mode hint was printed (the hint is informational, not a
    failure) or when the user-mode restart succeeded.
    """
    if mode is InstallMode.SYSTEM:
        _print_system_restart_hint()
        return 0
    return _restart_user_unit()


def cmd_restart(args: argparse.Namespace) -> None:
    del args  # the subcommand takes no flags today
    code = restart_or_hint(detect_mode())
    if code != 0:
        sys.exit(code)
