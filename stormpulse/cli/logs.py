"""CLI handler for ``stormpulse logs``: ``journalctl --user`` on the agent unit.

Read-only; a permission failure surfaces from journalctl itself. Defaults to
the last 100 lines then follow (``--no-follow`` dumps once). ``--since``,
``--until`` and ``--grep`` pass through; a bounded window drops the ``-n`` cap
and follow, since an incident wants the whole window then exit. Explicit
``-f``/``-n`` always win.
"""

from __future__ import annotations

import argparse
import subprocess
import sys


def _journalctl_argv(
    lines: int | None,
    *,
    follow: bool,
    since: str | None = None,
    until: str | None = None,
    grep: str | None = None,
) -> list[str]:
    """Build the journalctl invocation."""
    argv = ["journalctl", "--user", "-u", "stormpulse"]
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


def add_logs_subparser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    parser = subparsers.add_parser(
        "logs",
        help="tail the agent journal (journalctl --user wrapper)",
    )
    parser.add_argument(
        "-n",
        "--lines",
        type=int,
        default=None,
        help=(
            "number of lines to show (default: 100, or the whole window "
            "when --since/--until is given)"
        ),
    )
    parser.add_argument(
        "-f",
        "--follow",
        action="store_true",
        default=None,
        help="follow after printing (default unless --since/--until is given)",
    )
    parser.add_argument(
        "--no-follow",
        dest="follow",
        action="store_false",
        help="dump lines and exit instead of following",
    )
    parser.add_argument(
        "--since",
        metavar="WHEN",
        help='window start, journalctl syntax: "06:00", "1 hour ago", "2026-07-19 06:14"',
    )
    parser.add_argument(
        "--until",
        metavar="WHEN",
        help="window end, same syntax as --since",
    )
    parser.add_argument(
        "-g",
        "--grep",
        metavar="PATTERN",
        help=(
            "only lines whose message matches this PCRE "
            "(case-insensitive when all-lowercase); combines with follow"
        ),
    )
    parser.set_defaults(func=cmd_logs)
