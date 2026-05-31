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

The actual dispatch lives in ``stormpulse.init.system`` so the
feature-layer init flows (``garage init``, ``caddy init``,
``logging init``) can route through it without crossing the
import-linter layer contract (entry → features is forbidden).
"""

from __future__ import annotations

import argparse
import sys

from stormpulse.init.mode import detect_mode
from stormpulse.init.system import restart_or_hint


def cmd_restart(args: argparse.Namespace) -> None:
    del args  # the subcommand takes no flags today
    code = restart_or_hint(detect_mode())
    if code != 0:
        sys.exit(code)
