"""Transport-failure taxonomy for the hand-rolled S3 client.

A raw ``OSError``/``socket.timeout`` must never escape ``GarageS3Client``:
callers declare their failure contracts in terms of ``S3Error``, and the
clear-bucket leak trail depends on the exception staying in-family.
"""

from __future__ import annotations

import socket

import pytest

from stormpulse.garage.s3 import GarageS3Client, S3Error


def _closed_port() -> int:
    """Grab an ephemeral port and release it, so nothing is listening there."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_connection_refused_raises_s3error_not_oserror() -> None:
    client = GarageS3Client(
        endpoint=f"http://127.0.0.1:{_closed_port()}",
        region="garage",
        access_key="GKTEST",
        secret_key="secret",
    )
    with pytest.raises(S3Error, match="transport error"):
        client.list_objects_v2("some-bucket")
