"""Entry point for stormpulse agent."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import ssl
import sys
from pathlib import Path

from stormpulse import __version__

logger = logging.getLogger("stormpulse")

_DEFAULT_CONFIG = "/etc/stormpulse/stormpulse.toml"
_DEFAULT_CREDS_DIR = "/etc/stormpulse"


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="stormpulse",
        description="Storm Pulse agent — secure server management over WebSocket",
    )
    parser.add_argument(
        "--version", action="version", version=f"storm-pulse-agent v{__version__}",
    )
    subparsers = parser.add_subparsers(dest="command")

    # --- run subcommand ---
    run_parser = subparsers.add_parser("run", help="start the agent")
    run_parser.add_argument(
        "config",
        nargs="?",
        default=_DEFAULT_CONFIG,
        help=f"path to config file (default: {_DEFAULT_CONFIG})",
    )

    # --- enroll subcommand ---
    enroll_parser = subparsers.add_parser(
        "enroll", help="enroll this agent with the dashboard",
    )
    enroll_parser.add_argument("endpoint", help="enrollment URL")
    enroll_parser.add_argument("agent_id", help="unique agent identifier")
    enroll_parser.add_argument("token", help="one-time enrollment token from dashboard")
    enroll_parser.add_argument(
        "--creds-dir",
        default=_DEFAULT_CREDS_DIR,
        help=f"directory for credential files (default: {_DEFAULT_CREDS_DIR})",
    )
    enroll_parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing credential files",
    )

    # --- status subcommand ---
    status_parser = subparsers.add_parser("status", help="show agent status")
    status_parser.add_argument(
        "config",
        nargs="?",
        default=_DEFAULT_CONFIG,
        help=f"path to config file (default: {_DEFAULT_CONFIG})",
    )

    args = parser.parse_args()

    log_level = os.environ.get("STORMPULSE_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    if args.command == "enroll":
        _cmd_enroll(args)
    elif args.command == "run":
        _cmd_run(args)
    elif args.command == "status":
        _cmd_status(args)
    elif args.command is None:
        # Detect old syntax: stormpulse /path/to/config
        if len(sys.argv) == 2 and not sys.argv[1].startswith("-"):
            logger.error(
                "Unknown command: %s. Did you mean: stormpulse run %s",
                sys.argv[1], sys.argv[1],
            )
        else:
            print("Usage: stormpulse <command> [options]\n", file=sys.stderr)
            print("Commands:", file=sys.stderr)
            print("  run      Start the agent", file=sys.stderr)
            print("  enroll   Enroll this agent with the dashboard", file=sys.stderr)
            print("  status   Show agent status", file=sys.stderr)
            print(
                "\nRun 'stormpulse <command> --help' for details.",
                file=sys.stderr,
            )
        sys.exit(1)


def _cmd_enroll(args: argparse.Namespace) -> None:
    from stormpulse.enroll import (
        EnrollError,
        build_csr,
        generate_keypair,
        request_certificate,
        write_credentials,
    )

    creds_dir = Path(args.creds_dir)

    logger.info("Generating EC P-256 keypair...")
    private_key, key_pem = generate_keypair()

    logger.info("Building CSR for agent_id=%s", args.agent_id)
    csr_pem = build_csr(private_key, args.agent_id)

    logger.info("Requesting certificate from %s", args.endpoint)
    try:
        response = request_certificate(
            args.endpoint, args.agent_id, args.token, csr_pem,
        )
    except EnrollError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    logger.info("Writing credentials to %s", creds_dir)
    try:
        creds = write_credentials(creds_dir, key_pem, response, force=args.force)
    except EnrollError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    logger.info("Enrollment complete:")
    logger.info("  Client cert: %s", creds.client_cert)
    logger.info("  Client key:  %s", creds.client_key)
    logger.info("  CA cert:     %s", creds.ca_cert)
    logger.info("  HMAC key:    %s", creds.hmac_key)
    logger.info(
        "Next: edit %s/stormpulse.toml and start the agent with 'stormpulse run'",
        creds_dir,
    )


def _cmd_run(args: argparse.Namespace) -> None:
    from stormpulse.agent import Agent, create_ssl_context
    from stormpulse.auth import AuthError, NonceStore, load_hmac_secret
    from stormpulse.config import ConfigError, load_config
    from stormpulse.metrics import prime_cpu_percent

    config_path = Path(args.config)
    try:
        config = load_config(config_path)
        config.validate_paths()
    except ConfigError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    try:
        secret = load_hmac_secret(config.auth.hmac_secret)
    except AuthError as exc:
        logger.error("Auth setup error: %s", exc)
        sys.exit(1)

    nonce_store = NonceStore(config.storage.db_path)
    prime_cpu_percent()

    try:
        ssl_ctx = create_ssl_context(config.tls)
    except ssl.SSLError as exc:
        logger.error(
            "TLS setup failed: %s. Check that ca_cert, client_cert, and "
            "client_key in your config point to valid PEM files.",
            exc,
        )
        nonce_store.close()
        sys.exit(1)

    async def _run() -> None:
        shutdown = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, shutdown.set)

        agent = Agent(config, secret, nonce_store, ssl_ctx, shutdown)
        await agent.run()

    logger.info(
        "storm-pulse-agent v%s starting (agent_id=%s)", __version__, config.agent.id,
    )
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Received interrupt, shutting down")
    finally:
        nonce_store.close()
        logger.info("Agent stopped")


def _cmd_status(args: argparse.Namespace) -> None:
    from stormpulse.status import collect_status, print_status

    info = collect_status(Path(args.config))
    print_status(info)


if __name__ == "__main__":
    main()
