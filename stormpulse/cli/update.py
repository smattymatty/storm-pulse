"""CLI handler for ``stormpulse update``.

Wraps the canonical ``pipx install --force ...`` invocation so
operators don't have to memorize the git URL, the package name, or
the "don't trust ``pipx upgrade`` for this project" footgun. After
install the wrapper restarts the agent's user unit by default --
``pipx install --force`` replaces the binary on disk but the running
process keeps the old code in memory until restart, so auto-restart
is the correct default, not a cosmetic add-on.

No subprocess sudo. In system-mode contexts (EUID 0 or detected via
``stormpulse.init.mode.detect_mode``) the wrapper prints the restart
command for the operator to run, matching the no-escalation posture
of the install/init flow.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys

from stormpulse.init.mode import detect_mode
from stormpulse.init.system import restart_or_hint

logger = logging.getLogger("stormpulse")

GIT_URL = "git+https://git.stormdevelopments.ca/official-public/storm-pulse.git"
PACKAGE_NAME = "storm-pulse-agent"


def _build_pipx_argv(
    source: str,
    branch: str | None,
    version: str | None,
) -> list[str]:
    """Compose the pipx invocation for the chosen source.

    - ``git``: ``pipx install --force git+<URL>@<branch>``. Branch
      defaults to ``main`` when ``branch`` is ``None``.
    - ``pip``: ``pipx install --force <package>[==<version>]``.
    """
    if source == "git":
        ref = branch or "main"
        target = f"{GIT_URL}@{ref}"
    elif source == "pip":
        target = PACKAGE_NAME
        if version:
            target = f"{target}=={version}"
    else:
        raise ValueError(f"unknown source: {source!r}")
    return ["pipx", "install", "--force", target]


def cmd_update(args: argparse.Namespace) -> None:
    # Reject incompatible flag combos before doing any work.
    if args.branch is not None and args.source != "git":
        logger.error("--branch is only valid with --source git")
        sys.exit(2)
    if args.version is not None and args.source != "pip":
        logger.error("--version is only valid with --source pip")
        sys.exit(2)

    if shutil.which("pipx") is None:
        logger.error(
            "pipx not found on PATH. The agent is installed via pipx "
            "and update requires it. Install with: sudo apt install pipx",
        )
        sys.exit(1)

    argv = _build_pipx_argv(args.source, args.branch, args.version)
    target = argv[-1]
    print(f"Updating {PACKAGE_NAME} from {target}...", file=sys.stderr)

    try:
        result = subprocess.run(argv, timeout=600)
    except subprocess.TimeoutExpired:
        logger.error(
            "pipx did not finish within 600s. Check your network "
            "connection and retry. If pipx is genuinely slow on this "
            "host, raise the timeout by editing stormpulse/cli/update.py.",
        )
        sys.exit(1)
    if result.returncode != 0:
        logger.error("pipx exited %d; see output above", result.returncode)
        sys.exit(result.returncode)

    if not args.restart:
        print(
            "Skipping restart. When ready: systemctl --user restart stormpulse",
            file=sys.stderr,
        )
        return

    code = restart_or_hint(detect_mode())
    if code != 0:
        sys.exit(code)
