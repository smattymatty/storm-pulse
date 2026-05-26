"""Tests for ``stormpulse.agent.create_ssl_context``."""

from __future__ import annotations

import ssl
from pathlib import Path
from unittest.mock import MagicMock, patch

from stormpulse.agent import create_ssl_context
from stormpulse.config import TlsConfig


@patch("stormpulse.agent.ssl_context.ssl.create_default_context")
def test_create_ssl_context(mock_ctx_factory: MagicMock) -> None:
    mock_ctx = MagicMock(spec=ssl.SSLContext)
    mock_ctx_factory.return_value = mock_ctx
    tls = TlsConfig(
        ca_cert=Path("/ca.pem"),
        client_cert=Path("/agent.pem"),
        client_key=Path("/key.pem"),
    )

    result = create_ssl_context(tls)

    mock_ctx_factory.assert_called_once_with()
    mock_ctx.load_verify_locations.assert_called_once_with(cafile="/ca.pem")
    mock_ctx.load_cert_chain.assert_called_once_with(
        certfile="/agent.pem", keyfile="/key.pem",
    )
    assert result is mock_ctx
