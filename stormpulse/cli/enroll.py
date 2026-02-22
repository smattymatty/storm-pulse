"""CLI handler for ``stormpulse enroll``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logger = logging.getLogger("stormpulse")


def cmd_enroll(args: argparse.Namespace) -> None:
    from stormpulse.enroll import (
        EnrollError,
        build_csr,
        generate_keypair,
        request_certificate,
        write_credentials,
        write_enroll_metadata,
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

    try:
        write_enroll_metadata(creds_dir, args.endpoint, args.agent_id)
    except EnrollError:
        logger.warning("Could not write enroll.json — init defaults will be unavailable")

    logger.info("Enrollment complete:")
    logger.info("  Client cert: %s", creds.client_cert)
    logger.info("  Client key:  %s", creds.client_key)
    logger.info("  CA cert:     %s", creds.ca_cert)
    logger.info("  HMAC key:    %s", creds.hmac_key)
    logger.info(
        "Next: run 'stormpulse init' to generate config, or edit %s/stormpulse.toml manually",
        creds_dir,
    )
