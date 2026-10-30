"""CLI handler for ``stormpulse run``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import ssl
import sys
from pathlib import Path

from stormpulse import __version__
from stormpulse.init.files import default_config_path

logger = logging.getLogger("stormpulse")


def cmd_run(args: argparse.Namespace) -> None:
    from stormpulse.agent import Agent, create_ssl_context
    from stormpulse.auth import AuthError, NonceStore, load_hmac_secret
    from stormpulse.config import ConfigError, load_config
    from stormpulse.init.files import user_data_dir
    from stormpulse.logging import LogPositionStore, PulseLogger
    from stormpulse.metrics import prime_cpu_percent
    from stormpulse.signoff import SignoffState, state_dir_from_db_path

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
    log_position_store: LogPositionStore | None = None
    pulse_logger: PulseLogger | None = None
    if config.log_groups:
        log_position_store = LogPositionStore(config.storage.db_path)
        pulse_log_path = user_data_dir() / "agent.log"
        pulse_log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        pulse_logger = PulseLogger(pulse_log_path, config.agent.id)
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

        signoff_state = SignoffState(
            state_dir_from_db_path(config.storage.db_path),
        )
        agent = Agent(
            config,
            secret,
            nonce_store,
            ssl_ctx,
            shutdown,
            log_position_store=log_position_store,
            pulse_logger=pulse_logger,
            signoff_state=signoff_state,
        )
        await agent.run()

    logger.info(
        "storm-pulse-agent v%s starting (agent_id=%s)",
        __version__,
        config.agent.id,
    )
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("Received interrupt, shutting down")
    finally:
        nonce_store.close()
        if log_position_store is not None:
            log_position_store.close()
        logger.info("Agent stopped")


def add_run_subparser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    default_config = default_config_path()
    parser = subparsers.add_parser("run", help="start the agent")
    parser.add_argument(
        "config",
        nargs="?",
        default=default_config,
        help=f"path to config file (default: {default_config})",
    )
    parser.set_defaults(func=cmd_run)
