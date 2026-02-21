"""Entry point for stormpulse agent."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from stormpulse import __version__
from stormpulse.agent import Agent, create_ssl_context
from stormpulse.auth import AuthError, NonceStore, load_hmac_secret
from stormpulse.config import ConfigError, load_config
from stormpulse.metrics import prime_cpu_percent

logger = logging.getLogger("stormpulse")

_DEFAULT_CONFIG = "/etc/stormpulse/stormpulse.toml"


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="stormpulse",
        description="Storm Pulse agent — secure server management over WebSocket",
    )
    parser.add_argument(
        "config",
        nargs="?",
        default=_DEFAULT_CONFIG,
        help=f"path to config file (default: {_DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--version", action="version", version=f"storm-pulse-agent v{__version__}",
    )
    args = parser.parse_args()

    log_level = os.environ.get("STORMPULSE_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

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
    ssl_ctx = create_ssl_context(config.tls)

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
        pass
    finally:
        nonce_store.close()
        logger.info("Agent stopped")


if __name__ == "__main__":
    main()
