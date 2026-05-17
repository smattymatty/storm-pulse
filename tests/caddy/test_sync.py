"""Tests for the Caddy sync handler.

The bulk uses mocked ``http.client.HTTPConnection`` for consistency with
the existing subprocess.run mocking in garage handler tests. Two
integration tests spin up a real ``http.server.HTTPServer`` in a thread
to confirm the URL routing, content-type, and body encoding work
against a real socket.
"""

from __future__ import annotations

import asyncio
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from stormpulse.caddy.sync import (
    _atomic_write_or_remove,
    _post_caddy_load,
    make_caddy_sync_handler,
)
from stormpulse.config import CaddyConfig


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path) -> CaddyConfig:
    return CaddyConfig(
        enabled=True,
        admin_url="http://localhost:2019",
        main_caddyfile=tmp_path / "Caddyfile",
        drop_in_path=tmp_path / "conf.d" / "cellar-custom-domains.caddy",
    )


def _noop_progress() -> Any:
    async def progress(stage: str, current: int, total: int | None, message: str) -> None:
        return None
    return progress


async def _run_handler(handler: Any) -> Any:
    return await handler(_noop_progress())


# ---------------------------------------------------------------------------
# _atomic_write_or_remove — pure file I/O, no mocks needed
# ---------------------------------------------------------------------------


class TestAtomicWriteOrRemove:
    def test_writes_new_file(self, tmp_path: Path) -> None:
        path = tmp_path / "drop_in.caddy"
        _atomic_write_or_remove(path, "example.com { respond \"ok\" }\n")
        assert path.read_text() == "example.com { respond \"ok\" }\n"

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        path = tmp_path / "drop_in.caddy"
        path.write_text("old content")
        _atomic_write_or_remove(path, "new content")
        assert path.read_text() == "new content"

    def test_empty_fragment_removes_file(self, tmp_path: Path) -> None:
        path = tmp_path / "drop_in.caddy"
        path.write_text("stale content")
        _atomic_write_or_remove(path, "")
        assert not path.exists()

    def test_empty_fragment_when_no_file_is_noop(self, tmp_path: Path) -> None:
        path = tmp_path / "drop_in.caddy"
        _atomic_write_or_remove(path, "")
        assert not path.exists()

    def test_tmp_file_does_not_remain(self, tmp_path: Path) -> None:
        path = tmp_path / "drop_in.caddy"
        _atomic_write_or_remove(path, "content")
        # The .tmp file must not linger after a successful rename.
        assert not (tmp_path / "drop_in.caddy.tmp").exists()


# ---------------------------------------------------------------------------
# _post_caddy_load — mocked HTTPConnection
# ---------------------------------------------------------------------------


def _make_mock_connection(status: int = 200, body: str = "") -> Any:
    """Build a mock HTTPConnection with the given response status + body."""
    mock_response = MagicMock()
    mock_response.status = status
    mock_response.read.return_value = body.encode("utf-8")

    mock_conn = MagicMock()
    mock_conn.getresponse.return_value = mock_response

    return mock_conn


class TestPostCaddyLoad:
    def test_2xx_returns_success(self) -> None:
        mock_conn = _make_mock_connection(status=200)
        with patch(
            "stormpulse.caddy.sync.http.client.HTTPConnection",
            return_value=mock_conn,
        ):
            ok, err = _post_caddy_load("http://localhost:2019", "example.com {}")
        assert ok is True
        assert err == ""

    def test_4xx_returns_failure_with_body(self) -> None:
        mock_conn = _make_mock_connection(
            status=400, body="syntax error at line 3",
        )
        with patch(
            "stormpulse.caddy.sync.http.client.HTTPConnection",
            return_value=mock_conn,
        ):
            ok, err = _post_caddy_load(
                "http://localhost:2019", "garbage",
            )
        assert ok is False
        assert "400" in err
        assert "syntax error" in err

    def test_5xx_returns_failure(self) -> None:
        mock_conn = _make_mock_connection(status=500)
        with patch(
            "stormpulse.caddy.sync.http.client.HTTPConnection",
            return_value=mock_conn,
        ):
            ok, err = _post_caddy_load(
                "http://localhost:2019", "anything",
            )
        assert ok is False
        assert "500" in err

    def test_connection_error_returns_failure(self) -> None:
        with patch(
            "stormpulse.caddy.sync.http.client.HTTPConnection",
            side_effect=OSError("connection refused"),
        ):
            ok, err = _post_caddy_load(
                "http://localhost:2019", "anything",
            )
        assert ok is False
        assert "connection refused" in err

    def test_posts_to_load_endpoint(self) -> None:
        mock_conn = _make_mock_connection(status=200)
        with patch(
            "stormpulse.caddy.sync.http.client.HTTPConnection",
            return_value=mock_conn,
        ):
            _post_caddy_load("http://localhost:2019", "example.com {}")
        # Verify request was made to /load.
        method, path = mock_conn.request.call_args.args[:2]
        assert method == "POST"
        assert path == "/load"

    def test_sets_caddyfile_content_type(self) -> None:
        mock_conn = _make_mock_connection(status=200)
        with patch(
            "stormpulse.caddy.sync.http.client.HTTPConnection",
            return_value=mock_conn,
        ):
            _post_caddy_load("http://localhost:2019", "example.com {}")
        headers = mock_conn.request.call_args.kwargs["headers"]
        assert headers["Content-Type"] == "text/caddyfile"

    def test_invalid_scheme_rejected(self) -> None:
        ok, err = _post_caddy_load("ftp://localhost:2019", "x")
        assert ok is False
        assert "scheme" in err.lower()


# ---------------------------------------------------------------------------
# make_caddy_sync_handler — wires _post_caddy_load + _atomic_write
# ---------------------------------------------------------------------------


class TestCaddySyncHandler:
    def test_happy_path_writes_fragment(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        cfg.drop_in_path.parent.mkdir()

        mock_conn = _make_mock_connection(status=200)
        with patch(
            "stormpulse.caddy.sync.http.client.HTTPConnection",
            return_value=mock_conn,
        ):
            handler = make_caddy_sync_handler(
                cfg, {"region": "vancouver-1", "fragment": "example.com { }\n"},
            )
            outcome = asyncio.run(_run_handler(handler))

        assert outcome.success is True
        assert cfg.drop_in_path.read_text() == "example.com { }\n"
        assert outcome.extras["region"] == "vancouver-1"
        assert outcome.extras["removed"] is False

    def test_empty_fragment_removes_drop_in(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        cfg.drop_in_path.parent.mkdir()
        cfg.drop_in_path.write_text("stale content")

        mock_conn = _make_mock_connection(status=200)
        with patch(
            "stormpulse.caddy.sync.http.client.HTTPConnection",
            return_value=mock_conn,
        ):
            handler = make_caddy_sync_handler(
                cfg, {"region": "vancouver-1", "fragment": ""},
            )
            outcome = asyncio.run(_run_handler(handler))

        assert outcome.success is True
        assert not cfg.drop_in_path.exists()
        assert outcome.extras["removed"] is True

    def test_load_failure_does_not_write_fragment(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        cfg.drop_in_path.parent.mkdir()
        cfg.drop_in_path.write_text("previous good content")

        mock_conn = _make_mock_connection(
            status=400, body="syntax error",
        )
        with patch(
            "stormpulse.caddy.sync.http.client.HTTPConnection",
            return_value=mock_conn,
        ):
            handler = make_caddy_sync_handler(
                cfg, {"region": "vancouver-1", "fragment": "broken"},
            )
            outcome = asyncio.run(_run_handler(handler))

        assert outcome.success is False
        assert outcome.failure_reason == "reload_failed"
        # On-disk fragment unchanged — eventually consistent.
        assert cfg.drop_in_path.read_text() == "previous good content"

    def test_persist_failure_surfaces_after_successful_reload(
        self, tmp_path: Path,
    ) -> None:
        cfg = _make_config(tmp_path)
        # Don't create parent dir — write will fail.

        mock_conn = _make_mock_connection(status=200)
        with patch(
            "stormpulse.caddy.sync.http.client.HTTPConnection",
            return_value=mock_conn,
        ):
            handler = make_caddy_sync_handler(
                cfg, {"region": "vancouver-1", "fragment": "x"},
            )
            outcome = asyncio.run(_run_handler(handler))

        assert outcome.success is False
        assert outcome.failure_reason == "persist_failed"


# ---------------------------------------------------------------------------
# Integration: real http.server in a thread.
# Belt-and-suspenders on the mocked tests above — confirms URL routing,
# content-type, and body encoding survive a real socket round-trip.
# ---------------------------------------------------------------------------


class _CaddyAdminStub(BaseHTTPRequestHandler):
    """Tiny HTTP server stub that mimics Caddy admin's /load endpoint."""

    captured: dict[str, Any] = {}

    def do_POST(self) -> None:  # noqa: N802 — required by BaseHTTPRequestHandler
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        # Stash for assertion.
        type(self).captured = {
            "path": self.path,
            "content_type": self.headers.get("Content-Type", ""),
            "body": body,
        }
        if "REJECT" in body:
            self.send_response(400)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"rejected")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format: str, *args: Any) -> None:
        # Silence the noisy default access log during tests.
        return


@pytest.fixture
def stub_admin() -> Any:
    """Start a stub admin server on an ephemeral port; tear down after."""
    _CaddyAdminStub.captured = {}
    server = HTTPServer(("127.0.0.1", 0), _CaddyAdminStub)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", _CaddyAdminStub
    finally:
        server.shutdown()
        thread.join(timeout=5)


class TestIntegrationRealSocket:
    def test_real_post_round_trip(self, stub_admin: Any) -> None:
        admin_url, stub_cls = stub_admin
        fragment = "example.com {\n    respond \"ok\"\n}\n"
        ok, err = _post_caddy_load(admin_url, fragment)
        assert ok is True
        assert err == ""
        captured = stub_cls.captured
        assert captured["path"] == "/load"
        assert captured["content_type"] == "text/caddyfile"
        assert captured["body"] == fragment

    def test_real_load_rejection_surfaces_body(self, stub_admin: Any) -> None:
        admin_url, _stub_cls = stub_admin
        ok, err = _post_caddy_load(admin_url, "REJECT this")
        assert ok is False
        assert "400" in err
        assert "rejected" in err
