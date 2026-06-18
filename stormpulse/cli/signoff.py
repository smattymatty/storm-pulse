"""CLI handlers for the ``stormpulse signoff`` subcommand group.

The seal closes the dashboard's ``run_verify_block`` hatch on this
agent. The agent ships sealed; the operator unseals (with an
interactive hostname-typing confirmation) to verify, then reseals.
See ADR CORE-004 for the trust story.
"""

from __future__ import annotations

import argparse
import logging
import socket
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from stormpulse.signoff import SignoffState

from stormpulse.init.files import default_config_path

logger = logging.getLogger("stormpulse")

_DEFAULT_CONFIG = default_config_path()


def _load_state(args: argparse.Namespace) -> tuple[SignoffState, Path]:
    """Resolve config and return a SignoffState plus the state dir."""
    from stormpulse.config import ConfigError, load_config
    from stormpulse.signoff import SignoffState, state_dir_from_db_path

    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)
    state_dir = state_dir_from_db_path(config.storage.db_path)
    return SignoffState(state_dir), state_dir


def cmd_signoff_status(args: argparse.Namespace) -> None:
    from stormpulse.signoff import format_unsealed_duration

    state, state_dir = _load_state(args)
    if state.is_sealed():
        print(f"Sign-off: SEALED ({state.path})")
        print(
            "  run_verify_block is disabled on this agent. The dashboard's "
            "verify hatch is closed.",
        )
        print("  Unseal with: stormpulse signoff unseal")
        return

    since = state.unsealed_since()
    duration = format_unsealed_duration(since)
    # Red + bold on terminals that support it. Falls back gracefully when
    # the output is piped or redirected.
    banner = "\033[1;31m⚠  UNSEALED\033[0m" if sys.stdout.isatty() else "⚠ UNSEALED"
    print(f"Sign-off: {banner}  (unsealed for {duration})")
    print(f"  State dir: {state_dir}")
    print(
        "  run_verify_block is ENABLED. The dashboard can dispatch HMAC-signed "
        "shell to this agent until you re-seal.",
    )
    print(
        "  Persistence implanted during the unsealed window survives reseal - "
        "treat this window as elevated risk.",
    )
    print("  Reseal with: stormpulse signoff seal")


def cmd_signoff_seal(args: argparse.Namespace) -> None:
    state, _ = _load_state(args)
    if state.seal():
        print(f"Sealed. Verify-block dispatch is now disabled ({state.path}).")
        print(
            "Effective immediately for newly-arriving commands. The dashboard "
            "learns the new state on the agent's next reconnect.",
        )
    else:
        print(f"Already sealed ({state.path}). No change.")


def cmd_signoff_unseal(args: argparse.Namespace) -> None:
    """Unseal the agent after explicit operator confirmation.

    Requires the operator to type this host's hostname back at the
    prompt - pasting a one-liner from a doc shouldn't unseal anything.
    For automation use ``--confirm-hostname HOSTNAME`` to skip the
    interactive prompt; tests and CI pipelines should use that path
    explicitly so the friction is visible in the script.
    """
    state, _ = _load_state(args)
    if not state.is_sealed():
        print(f"Already unsealed ({state.path}). No change.")
        return

    hostname = socket.gethostname()
    explicit = getattr(args, "confirm_hostname", None)

    if explicit is None:
        if not sys.stdin.isatty():
            print(
                "stormpulse signoff unseal requires interactive confirmation "
                "or the --confirm-hostname HOSTNAME flag for automation.",
                file=sys.stderr,
            )
            sys.exit(2)
        explicit = _interactive_confirm(hostname)

    if explicit != hostname:
        print(
            f"Hostname confirmation did not match (expected {hostname!r}, "
            f"got {explicit!r}). No change.",
            file=sys.stderr,
        )
        sys.exit(1)

    state.unseal()
    logger.warning(
        "signoff_unsealed host=%s state_path=%s",
        hostname,
        state.path,
    )
    print(f"Unsealed. Verify-block dispatch is RE-ENABLED ({state.path}).")
    print(
        "Reseal as soon as verification is done. While unsealed the dashboard "
        "can run arbitrary HMAC-signed shell on this host.",
    )
    print("  Reseal with: stormpulse signoff seal")


def _interactive_confirm(hostname: str) -> str:
    """Print the unseal warning and read the hostname confirmation."""
    bold = "\033[1m" if sys.stdout.isatty() else ""
    red = "\033[1;31m" if sys.stdout.isatty() else ""
    reset = "\033[0m" if sys.stdout.isatty() else ""
    print(
        f"\n{red}⚠  Unsealing re-opens dashboard verify-block dispatch.{reset}\n"
        f"\n"
        f"  This command will allow the Storm Developments dashboard to\n"
        f"  dispatch arbitrary HMAC-signed shell against this host until\n"
        f"  you reseal.\n"
        f"\n"
        f"  By unsealing you acknowledge:\n"
        f"    • The dashboard gains remote code execution on this host.\n"
        f"    • Persistence implanted during the unsealed window survives\n"
        f"      reseal. Reseal is a {bold}kill switch{reset}, not a recovery.\n"
        f"    • This action will be logged. Reseal as soon as verification\n"
        f"      is complete.\n"
        f"\n"
        f"  Type this agent's hostname to confirm:\n"
        f"  (expected: {bold}{hostname}{reset})\n"
    )
    try:
        return input("> ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted. No change.", file=sys.stderr)
        sys.exit(1)


def add_signoff_subparser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Add the ``signoff`` subcommand group with seal/unseal/status."""
    signoff_parser = subparsers.add_parser(
        "signoff",
        help="manage the dashboard's verify-block seal (ADR CORE-004)",
    )
    signoff_sub = signoff_parser.add_subparsers(dest="signoff_command")

    for name, help_text in (
        ("status", "show whether verify-block dispatch is sealed"),
        ("seal", "disable verify-block dispatch on this agent"),
        ("unseal", "re-enable verify-block dispatch (requires confirmation)"),
    ):
        p = signoff_sub.add_parser(name, help=help_text)
        p.add_argument(
            "--config",
            default=_DEFAULT_CONFIG,
            help=f"path to stormpulse.toml (default: {_DEFAULT_CONFIG})",
        )
        if name == "unseal":
            p.add_argument(
                "--confirm-hostname",
                default=None,
                help=(
                    "skip the interactive prompt by supplying the host's "
                    "hostname (for automation; humans should let the "
                    "interactive prompt run)"
                ),
            )
