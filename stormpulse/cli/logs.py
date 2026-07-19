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

Incident-window flags pass through to journalctl: ``--since`` /
``--until`` accept anything journalctl does ("06:00", "1 hour ago",
"2026-07-19 06:14"), and ``--grep`` is journalctl's PCRE message
filter (case-insensitive when the pattern is all-lowercase). A
bounded window changes the defaults: the ``-n`` cap and follow both
drop away, because "the whole window, then exit" is what an incident
reconstruction wants. ``-f``/``-n`` given explicitly always win.
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from stormpulse.init.mode import InstallMode, detect_mode


def _journalctl_argv(
    mode: InstallMode,
    lines: int | None,
    follow: bool,
    since: str | None = None,
    until: str | None = None,
    grep: str | None = None,
) -> list[str]:
    """Build the journalctl invocation for the chosen mode."""
    argv = ["journalctl"]
    if mode is InstallMode.USER:
        argv.append("--user")
    argv += ["-u", "stormpulse"]
    if lines is not None:
        argv += ["-n", str(lines)]
    if since is not None:
        argv += ["--since", since]
    if until is not None:
        argv += ["--until", until]
    if grep is not None:
        argv += ["-g", grep]
    if follow:
        argv.append("-f")
    return argv


def _resolve_view(args: argparse.Namespace) -> tuple[int | None, bool]:
    """Resolve the (lines, follow) defaults from what the operator asked for.

    Unbounded (no --since/--until): last 100 lines then follow, the
    historical default. Bounded: the full window, one shot - a tail cap
    or a live follow would silently hide the very range the operator
    named. Explicit -n / -f / --no-follow override either way.
    """
    bounded = args.since is not None or args.until is not None
    lines = args.lines if args.lines is not None else (None if bounded else 100)
    follow = args.follow if args.follow is not None else not bounded
    return lines, follow


def cmd_logs(args: argparse.Namespace) -> None:
    """``stormpulse logs`` - tail or window the agent journal."""
    lines, follow = _resolve_view(args)
    argv = _journalctl_argv(
        detect_mode(),
        lines=lines,
        follow=follow,
        since=args.since,
        until=args.until,
        grep=args.grep,
    )
    filtered = args.since is not None or args.until is not None or args.grep is not None
    try:
        if follow:
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
                if filtered:
                    print(
                        "No stormpulse log lines matched the given window/pattern.",
                        file=sys.stderr,
                    )
                else:
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
