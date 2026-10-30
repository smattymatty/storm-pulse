"""CLI handler for ``stormpulse renew``: one renewal now, ignoring T-30.

Writes files only; a running agent loads the new pair at its next daily
check by serial (ADR CORE-010).
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from stormpulse.agent.ssl_context import create_ssl_context
from stormpulse.config import ConfigError, load_config
from stormpulse.enroll import (
    RenewError,
    days_remaining,
    read_cert_not_after,
    renew_certificate,
)
from stormpulse.init.files import default_config_path


def _days_left(cert: Path) -> str:
    not_after = read_cert_not_after(cert)
    if not_after is None:
        return "unknown days"
    return f"{days_remaining(not_after, datetime.now(UTC))} days"


def cmd_renew(args: argparse.Namespace) -> None:
    try:
        config = load_config(Path(args.config))
        tls = config.tls
        print(f"Client cert {tls.client_cert}: {_days_left(tls.client_cert)} remaining")
        renew_certificate(
            tls, config.agent.id, config.dashboard.url, create_ssl_context(tls)
        )
    except RenewError as exc:
        sys.exit(f"Renewal failed ({exc.reason}): {exc}")
    except (ConfigError, OSError) as exc:
        sys.exit(f"Renewal failed: {exc}")
    print(f"Renewed: {_days_left(tls.client_cert)} remaining")


def add_renew_subparser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    default_config = default_config_path()
    parser = subparsers.add_parser(
        "renew",
        help="renew the client cert now (CORE-010), ignoring the 30-day window",
    )
    parser.add_argument(
        "config",
        nargs="?",
        default=default_config,
        help=f"path to config file (default: {default_config})",
    )
    parser.set_defaults(func=cmd_renew)
