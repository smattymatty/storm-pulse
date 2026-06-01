"""CLI handler for ``stormpulse logs``.

Wraps ``journalctl`` for the agent's systemd unit so the operator
doesn't have to remember whether it's ``--user`` or not. Mode-aware
via ``detect_mode()``: USER installs hit the per-user journal,
SYSTEM installs hit the system journal.

Read-only. Unlike the install/init/update flow, there is no
no-escalation concern here: journalctl read access is typically
granted to the operator on Storm boxes (``adm`` or
``systemd-journal`` group); a permission failure surfaces from
journalctl itself rather than being pre-empted by this wrapper.

Default invocation shows the last 100 lines and then follows, so
``stormpulse logs`` gives both recent context and live tail in one
command. ``--no-follow`` switches to a one-shot dump.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from stormpulse.init.mode import InstallMode, detect_mode


def _journalctl_argv(mode: InstallMode, lines: int, follow: bool) -> list[str]:
    """Build the journalctl invocation for the chosen mode."""
    argv = ["journalctl"]
    if mode is InstallMode.USER:
        argv.append("--user")
    argv += ["-u", "stormpulse", "-n", str(lines)]
    if follow:
        argv.append("-f")
    return argv


def cmd_logs(args: argparse.Namespace) -> None:
    """``stormpulse logs`` - tail the agent journal."""
    argv = _journalctl_argv(
        detect_mode(),
        lines=args.lines,
        follow=args.follow,
    )
    try:
        if args.follow:
            returncode = subprocess.run(argv).returncode
        else:
            # Capture so we can hint when the unit hasn't logged anything yet
            # (journalctl returns 0 with empty stdout, leaving the operator
            # wondering if the wrapper is broken).
            text_result = subprocess.run(argv, capture_output=True, text=True)
            if text_result.stdout:
                sys.stdout.write(text_result.stdout)
            if text_result.stderr:
                sys.stderr.write(text_result.stderr)
            if not text_result.stdout and text_result.returncode == 0:
                print(
                    "No stormpulse logs found. Has the unit been started? "
                    "Try: stormpulse init",
                    file=sys.stderr,
                )
            returncode = text_result.returncode
    except FileNotFoundError:
        print(
            "journalctl not found. Is systemd installed?",
            file=sys.stderr,
        )
        sys.exit(1)
    if returncode != 0:
        sys.exit(returncode)
