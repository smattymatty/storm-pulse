"""CLI composition root: root gate, one parser from every registrar, dispatch."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from stormpulse import __version__
from stormpulse.cli.caddy import add_caddy_subparser
from stormpulse.cli.config_check import add_config_subparser
from stormpulse.cli.enroll import add_enroll_subparser
from stormpulse.cli.garage import add_garage_subparser
from stormpulse.cli.init import add_init_subparser
from stormpulse.cli.integration import add_integration_subparser
from stormpulse.cli.investigate import add_investigate_subparser
from stormpulse.cli.log import add_logging_subparser
from stormpulse.cli.logs import add_logs_subparser
from stormpulse.cli.rclone import add_rclone_subparser
from stormpulse.cli.renew import add_renew_subparser
from stormpulse.cli.restart import add_restart_subparser
from stormpulse.cli.run import add_run_subparser
from stormpulse.cli.signoff import add_signoff_subparser
from stormpulse.cli.status import add_status_subparser
from stormpulse.cli.update import add_update_subparser
from stormpulse.init.checks import InitError, refuse_root

# `stormpulse` with no command prints argparse's view of this tuple:
# no second list to keep in step.
REGISTRARS = (
    add_run_subparser,
    add_enroll_subparser,
    add_init_subparser,
    add_update_subparser,
    add_restart_subparser,
    add_logs_subparser,
    add_status_subparser,
    add_renew_subparser,
    add_config_subparser,
    add_investigate_subparser,
    add_garage_subparser,
    add_caddy_subparser,
    add_rclone_subparser,
    add_logging_subparser,
    add_signoff_subparser,
    add_integration_subparser,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stormpulse",
        description="Storm Pulse agent - secure server management over WebSocket",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"storm-pulse-agent v{__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")
    for register in REGISTRARS:
        register(subparsers)
    return parser


def main() -> None:
    # One gate for every command: run as root, any of them leaves files the
    # rootless agent cannot read.
    try:
        refuse_root(os.geteuid())
    except InitError as exc:
        sys.exit(str(exc))
    parser = build_parser()
    args = parser.parse_args()

    log_level = os.environ.get("STORMPULSE_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    if args.command is None:
        parser.exit(1, parser.format_help())
    args.func(args)
