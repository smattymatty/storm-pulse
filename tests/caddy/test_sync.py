"""Tests for the Caddy sync handler.

The bulk uses mocked ``http.client.HTTPConnection`` for consistency with
the existing subprocess.run mocking in garage handler tests. Two
integration tests spin up a real ``http.server.HTTPServer`` in a thread
to confirm the URL routing, content-type, and body encoding work
against a real socket.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from stormpulse.caddy.config import CaddyConfig
from stormpulse.caddy.sync import (
    _PER_TENANT_MAX_BYTES,
    _atomic_write_or_remove,
    _decode_manifest,
    _plan_reconcile,
    _post_caddy_load,
    _read_and_absolutize_imports,
    make_caddy_sync_handler,
)


def _params(tenants: dict[str, str], *, region: str = "vancouver-1",
            authorize_bulk: bool = False) -> dict[str, str]:
    """Build the string-valued param dict the handler receives off the wire."""
    return {
        "region": region,
        "tenants": json.dumps(tenants),
        "authorize_bulk": "true" if authorize_bulk else "false",
    }

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(
    tmp_path: Path, main_caddyfile_content: str = "# stub\n"
) -> CaddyConfig:
    """Build a CaddyConfig and write the main Caddyfile to disk.

    Handler reads the main Caddyfile during reload; tests need it to
    exist. Default content is a comment - Caddy accepts an empty/
    comment-only config, which is enough for the reload mock to be
    exercised without coupling tests to a specific Caddyfile shape.
    """
    main_caddyfile = tmp_path / "Caddyfile"
    main_caddyfile.write_text(main_caddyfile_content)
    return CaddyConfig(
        enabled=True,
        admin_url="http://localhost:2019",
        main_caddyfile=main_caddyfile,
        drop_in_path=tmp_path / "conf.d" / "buckets-custom-domains.caddy",
    )


def _noop_progress() -> Any:
    async def progress(
        stage: str, current: int, total: int | None, message: str
    ) -> None:
        return None

    return progress


async def _run_handler(handler: Any) -> Any:
    return await handler(_noop_progress())


# ---------------------------------------------------------------------------
# _atomic_write_or_remove - pure file I/O, no mocks needed
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
# _post_caddy_load - mocked HTTPConnection
# ---------------------------------------------------------------------------


def _make_mock_connection(status: int = 200, body: str = "") -> Any:
    """Build a mock HTTPConnection with the given response status + body."""
    mock_response = MagicMock()
    mock_response.status = status
    mock_response.read.return_value = body.encode("utf-8")

    mock_conn = MagicMock()
    mock_conn.getresponse.return_value = mock_response

    return mock_conn


def _make_mock_connection_sequence(*responses: tuple[int, str]) -> Any:
    """Mock HTTPConnection whose successive requests get successive
    (status, body) responses - lets a test give /adapt and /load
    different answers."""
    mocks = []
    for status, body in responses:
        r = MagicMock()
        r.status = status
        r.read.return_value = body.encode("utf-8")
        mocks.append(r)
    mock_conn = MagicMock()
    mock_conn.getresponse.side_effect = mocks
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
            status=400,
            body="syntax error at line 3",
        )
        with patch(
            "stormpulse.caddy.sync.http.client.HTTPConnection",
            return_value=mock_conn,
        ):
            ok, err = _post_caddy_load(
                "http://localhost:2019",
                "garbage",
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
                "http://localhost:2019",
                "anything",
            )
        assert ok is False
        assert "500" in err

    def test_connection_error_returns_failure(self) -> None:
        with patch(
            "stormpulse.caddy.sync.http.client.HTTPConnection",
            side_effect=OSError("connection refused"),
        ):
            ok, err = _post_caddy_load(
                "http://localhost:2019",
                "anything",
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
# make_caddy_sync_handler - wires _post_caddy_load + _atomic_write
# ---------------------------------------------------------------------------


class TestCaddySyncHandler:
    def test_happy_path_writes_tenant_file(self, tmp_path: Path) -> None:
        cfg = _make_config(tmp_path)
        drop_in_dir = cfg.drop_in_path.parent
        drop_in_dir.mkdir()

        mock_conn = _make_mock_connection(status=200)
        with patch(
            "stormpulse.caddy.sync.http.client.HTTPConnection",
            return_value=mock_conn,
        ):
            handler = make_caddy_sync_handler(
                cfg,
                _params({"abcdef0123456789": "example.com { }\n"}),
            )
            outcome = asyncio.run(_run_handler(handler))

        assert outcome.success is True
        # One file per serving bucket, keyed by id.
        assert (drop_in_dir / "site-abcdef0123456789.caddy").read_text() == (
            "example.com { }\n"
        )
        assert outcome.extras["region"] == "vancouver-1"
        assert outcome.extras["tenants"] == 1
        assert outcome.extras["deleted"] == 0
        assert outcome.extras["rail_tripped"] is False

    def test_same_region_syncs_serialize(self, tmp_path: Path) -> None:
        """Regression, 2026-07-04 (the events plane's first live catch):
        two same-second syncs of one region shared site-<id>.caddy.tmp
        and the loser's os.replace hit Errno 2. A sync is a full
        read-modify-write of the region's drop-in set, so same-region
        syncs hold a per-region lock: persist sections never overlap
        and both syncs succeed."""
        import time as time_mod

        from stormpulse.caddy import sync as sync_module

        cfg = _make_config(tmp_path)
        drop_in_dir = cfg.drop_in_path.parent
        drop_in_dir.mkdir()

        active = 0
        max_active = 0
        gauge = threading.Lock()
        real_write = sync_module._atomic_write_or_remove

        def slow_write(path: Path, fragment: str) -> None:
            nonlocal active, max_active
            with gauge:
                active += 1
                max_active = max(max_active, active)
            time_mod.sleep(0.05)
            try:
                real_write(path, fragment)
            finally:
                with gauge:
                    active -= 1

        params = _params({"abcdef0123456789": "example.com { }\n"})

        async def run_two() -> tuple[Any, Any]:
            h1 = make_caddy_sync_handler(cfg, params)
            h2 = make_caddy_sync_handler(cfg, params)
            return await asyncio.gather(_run_handler(h1), _run_handler(h2))

        with patch(
            "stormpulse.caddy.sync.http.client.HTTPConnection",
            return_value=_make_mock_connection(status=200),
        ), patch.object(sync_module, "_atomic_write_or_remove", slow_write):
            out1, out2 = asyncio.run(run_two())

        assert out1.success is True
        assert out2.success is True
        assert max_active == 1

    def test_different_region_labels_still_serialize(self, tmp_path: Path) -> None:
        """The lock guards the drop-in directory, never the caller's region
        param: two dispatches naming different regions (or omitting one)
        reconcile the same directory and must not overlap."""
        import time as time_mod

        from stormpulse.caddy import sync as sync_module

        cfg = _make_config(tmp_path)
        drop_in_dir = cfg.drop_in_path.parent
        drop_in_dir.mkdir()

        active = 0
        max_active = 0
        gauge = threading.Lock()
        real_write = sync_module._atomic_write_or_remove

        def slow_write(path: Path, fragment: str) -> None:
            nonlocal active, max_active
            with gauge:
                active += 1
                max_active = max(max_active, active)
            time_mod.sleep(0.05)
            try:
                real_write(path, fragment)
            finally:
                with gauge:
                    active -= 1

        tenants = {"abcdef0123456789": "example.com { }\n"}

        async def run_two() -> tuple[Any, Any]:
            h1 = make_caddy_sync_handler(cfg, _params(tenants, region="vancouver-1"))
            h2 = make_caddy_sync_handler(cfg, _params(tenants, region="toronto-1"))
            return await asyncio.gather(_run_handler(h1), _run_handler(h2))

        with patch(
            "stormpulse.caddy.sync.http.client.HTTPConnection",
            return_value=_make_mock_connection(status=200),
        ), patch.object(sync_module, "_atomic_write_or_remove", slow_write):
            out1, out2 = asyncio.run(run_two())

        assert out1.success is True
        assert out2.success is True
        assert max_active == 1

    def test_empty_manifest_removes_single_managed_file(
        self, tmp_path: Path,
    ) -> None:
        """An empty manifest with one managed file on disk removes it: a
        single delete is within the inline cadence, so the rail allows it."""
        cfg = _make_config(tmp_path)
        drop_in_dir = cfg.drop_in_path.parent
        drop_in_dir.mkdir()
        stale = drop_in_dir / "site-deadbeefdeadbeef.caddy"
        stale.write_text("stale content")

        mock_conn = _make_mock_connection(status=200)
        with patch(
            "stormpulse.caddy.sync.http.client.HTTPConnection",
            return_value=mock_conn,
        ):
            handler = make_caddy_sync_handler(cfg, _params({}))
            outcome = asyncio.run(_run_handler(handler))

        assert outcome.success is True
        assert not stale.exists()
        assert outcome.extras["deleted"] == 1

    def test_invalid_config_named_at_preflight(self, tmp_path: Path) -> None:
        """Adapter errors are caught by /adapt and come back as a named,
        self-diagnosing config_invalid failure; /load is never attempted.

        Regression, 2026-06-11: a missing import target and a duplicate
        site definition each failed /load with a 400 that surfaced
        nowhere useful. The preflight names the failure in the command
        result and guarantees the running Caddy was never touched.
        """
        cfg = _make_config(tmp_path)
        drop_in_dir = cfg.drop_in_path.parent
        drop_in_dir.mkdir()

        mock_conn = _make_mock_connection(
            status=400,
            body="ambiguous site definition: mathew.stormsites.ca",
        )
        with patch(
            "stormpulse.caddy.sync.http.client.HTTPConnection",
            return_value=mock_conn,
        ):
            handler = make_caddy_sync_handler(
                cfg,
                _params({"abcdef0123456789": "new content\n"}),
            )
            outcome = asyncio.run(_run_handler(handler))

        assert outcome.success is False
        assert outcome.failure_reason == "config_invalid"
        assert "ambiguous site definition" in outcome.stderr
        assert "running Caddy is untouched" in outcome.stderr
        # Only the /adapt preflight was attempted, never /load.
        endpoints = [c.args[1] for c in mock_conn.request.call_args_list]
        assert endpoints == ["/adapt"]
        # File WAS written - disk-truth even when live didn't accept.
        assert (drop_in_dir / "site-abcdef0123456789.caddy").read_text() == (
            "new content\n"
        )

    def test_reload_failure_leaves_disk_updated(self, tmp_path: Path) -> None:
        """Persist happens first; a true reload failure (preflight passed,
        /load rejected) leaves disk newer than live."""
        cfg = _make_config(tmp_path)
        drop_in_dir = cfg.drop_in_path.parent
        drop_in_dir.mkdir()

        mock_conn = _make_mock_connection_sequence(
            (200, "{}"),              # /adapt accepts
            (500, "loader exploded"),  # /load rejects
        )
        with patch(
            "stormpulse.caddy.sync.http.client.HTTPConnection",
            return_value=mock_conn,
        ):
            handler = make_caddy_sync_handler(
                cfg,
                _params({"abcdef0123456789": "new content\n"}),
            )
            outcome = asyncio.run(_run_handler(handler))

        assert outcome.success is False
        assert outcome.failure_reason == "reload_failed"
        endpoints = [c.args[1] for c in mock_conn.request.call_args_list]
        assert endpoints == ["/adapt", "/load"]
        # File WAS written - disk-truth even when live didn't accept.
        # Next successful sync (or operator-initiated restart) restores
        # consistency.
        assert (drop_in_dir / "site-abcdef0123456789.caddy").read_text() == (
            "new content\n"
        )

    def test_persist_failure_skips_reload(self, tmp_path: Path) -> None:
        """If persist fails, reload is never attempted."""
        cfg = _make_config(tmp_path)
        # Don't create parent dir - write will fail.

        with patch(
            "stormpulse.caddy.sync.http.client.HTTPConnection",
        ) as mock_conn_cls:
            handler = make_caddy_sync_handler(
                cfg,
                _params({"abcdef0123456789": "x"}),
            )
            outcome = asyncio.run(_run_handler(handler))

        assert outcome.success is False
        assert outcome.failure_reason == "persist_failed"
        # Reload was never attempted - no HTTPConnection construction.
        mock_conn_cls.assert_not_called()

    def test_bad_manifest_rejected_before_disk(self, tmp_path: Path) -> None:
        """A malformed manifest is refused before any disk mutation or
        reload: a Storm-side render bug must not write a partial set."""
        cfg = _make_config(tmp_path)
        cfg.drop_in_path.parent.mkdir()

        with patch(
            "stormpulse.caddy.sync.http.client.HTTPConnection",
        ) as mock_conn_cls:
            handler = make_caddy_sync_handler(
                cfg,
                {"region": "vancouver-1", "tenants": "not json"},
            )
            outcome = asyncio.run(_run_handler(handler))

        assert outcome.success is False
        assert outcome.failure_reason == "config_invalid"
        assert "not valid JSON" in outcome.stderr
        mock_conn_cls.assert_not_called()
        # Nothing was written.
        assert list(cfg.drop_in_path.parent.glob("site-*.caddy")) == []

    def test_post_body_is_main_caddyfile_not_fragment(
        self,
        tmp_path: Path,
    ) -> None:
        """The /load body is the composed main Caddyfile, not a fragment.

        Posting just a fragment would replace the whole running config
        (Caddy /load is full-config). The agent must POST the main
        Caddyfile so Caddy re-adapts the composed config from disk.
        """
        main_content = (
            "{\n    admin localhost:2019\n}\n\nimport /tmp/storm/conf.d/*.caddy\n"
        )
        cfg = _make_config(tmp_path, main_caddyfile_content=main_content)
        cfg.drop_in_path.parent.mkdir()

        mock_conn = _make_mock_connection(status=200)
        with patch(
            "stormpulse.caddy.sync.http.client.HTTPConnection",
            return_value=mock_conn,
        ):
            handler = make_caddy_sync_handler(
                cfg,
                _params({"abcdef0123456789": "example.com { }\n"}),
            )
            outcome = asyncio.run(_run_handler(handler))

        assert outcome.success is True
        posted_body = mock_conn.request.call_args.kwargs["body"]
        # Main Caddyfile content is in the body…
        assert b"admin localhost:2019" in posted_body
        # …and the per-bucket fragment is NOT (it lives on disk).
        assert b"example.com" not in posted_body

    def test_main_caddyfile_read_failure_returns_reload_failed(
        self,
        tmp_path: Path,
    ) -> None:
        """If main Caddyfile is missing at reload time, surfaces reload_failed.

        Files are still written (disk-truth) - the next sync or
        operator-fixed Caddyfile recovers cleanly.
        """
        cfg = CaddyConfig(
            enabled=True,
            admin_url="http://localhost:2019",
            main_caddyfile=tmp_path / "missing-Caddyfile",
            drop_in_path=tmp_path / "conf.d" / "buckets-custom-domains.caddy",
        )
        drop_in_dir = cfg.drop_in_path.parent
        drop_in_dir.mkdir()

        handler = make_caddy_sync_handler(
            cfg,
            _params({"abcdef0123456789": "x"}),
        )
        outcome = asyncio.run(_run_handler(handler))

        assert outcome.success is False
        assert outcome.failure_reason == "reload_failed"
        # File was still written - disk-truth.
        assert (drop_in_dir / "site-abcdef0123456789.caddy").read_text() == "x"


# ---------------------------------------------------------------------------
# _decode_manifest - pure JSON + safety validation, no mocks
# ---------------------------------------------------------------------------


class TestDecodeManifest:
    def test_valid_manifest(self) -> None:
        raw = json.dumps({"abcdef0123456789": "example.com { }\n"})
        manifest, err = _decode_manifest(raw)
        assert err is None
        assert manifest == {"abcdef0123456789": "example.com { }\n"}

    def test_bad_json_rejected(self) -> None:
        manifest, err = _decode_manifest("not json")
        assert manifest is None
        assert err is not None and "not valid JSON" in err

    def test_non_object_rejected(self) -> None:
        manifest, err = _decode_manifest("[1, 2, 3]")
        assert manifest is None
        assert err is not None and "must be a JSON object" in err

    def test_non_string_value_rejected(self) -> None:
        manifest, err = _decode_manifest('{"abc": 123}')
        assert manifest is None
        assert err is not None and "must be strings" in err

    def test_unsafe_key_rejected(self) -> None:
        # A key feeds a filename; a path separator must never pass.
        manifest, err = _decode_manifest('{"../../etc/passwd": "x"}')
        assert manifest is None
        assert err is not None and "safe filename" in err

    def test_dotted_key_rejected(self) -> None:
        # Dots are barred too, so a key can never form a `..` traversal.
        manifest, err = _decode_manifest('{"a.b": "x"}')
        assert manifest is None
        assert err is not None and "safe filename" in err

    def test_oversize_fragment_rejected(self) -> None:
        oversize = "a" * (_PER_TENANT_MAX_BYTES + 1)
        manifest, err = _decode_manifest(json.dumps({"abc": oversize}))
        assert manifest is None
        assert err is not None and "exceeds per-bucket cap" in err


# ---------------------------------------------------------------------------
# _plan_reconcile - the delete rail's heart, pure, no I/O
# ---------------------------------------------------------------------------


class TestPlanReconcile:
    def test_writes_are_keyed_by_id(self) -> None:
        plan = _plan_reconcile(
            tenants={"aaaa": "x\n", "bbbb": "y\n"},
            on_disk=set(),
            legacy_name=None,
            legacy_exists=False,
            authorize_bulk=False,
        )
        assert plan.writes == {"site-aaaa.caddy": "x\n", "site-bbbb.caddy": "y\n"}
        assert plan.deletes == []
        assert plan.rail_tripped is False

    def test_single_delete_within_cadence_allowed(self) -> None:
        plan = _plan_reconcile(
            tenants={"aaaa": "x\n"},
            on_disk={"site-aaaa.caddy", "site-bbbb.caddy"},
            legacy_name=None,
            legacy_exists=False,
            authorize_bulk=False,
        )
        # bbbb left the manifest; one delete is within cadence.
        assert plan.deletes == ["site-bbbb.caddy"]
        assert plan.rail_tripped is False

    def test_mass_delete_trips_rail(self) -> None:
        # Reference is the on-disk set, NOT a count Storm sends: three files
        # on disk, a manifest naming one, means two would be deleted.
        plan = _plan_reconcile(
            tenants={"aaaa": "x\n"},
            on_disk={"site-aaaa.caddy", "site-bbbb.caddy", "site-cccc.caddy"},
            legacy_name=None,
            legacy_exists=False,
            authorize_bulk=False,
        )
        assert plan.rail_tripped is True
        assert plan.deletes == []  # ALL deletes skipped, not a subset.
        assert plan.skipped_deletes == ["site-bbbb.caddy", "site-cccc.caddy"]
        # Writes still flow - the safe direction.
        assert plan.writes == {"site-aaaa.caddy": "x\n"}

    def test_authorize_bulk_permits_mass_delete(self) -> None:
        plan = _plan_reconcile(
            tenants={"aaaa": "x\n"},
            on_disk={"site-aaaa.caddy", "site-bbbb.caddy", "site-cccc.caddy"},
            legacy_name=None,
            legacy_exists=False,
            authorize_bulk=True,
        )
        assert plan.rail_tripped is False
        assert plan.deletes == ["site-bbbb.caddy", "site-cccc.caddy"]

    def test_legacy_cutover_is_a_single_allowed_delete(self) -> None:
        # First cutover: no managed files yet, the legacy single-file
        # monolith present. Removing it is one delete, within cadence, so it
        # rides through the same sync that writes the per-bucket files.
        plan = _plan_reconcile(
            tenants={"aaaa": "x\n", "bbbb": "y\n"},
            on_disk=set(),
            legacy_name="buckets-custom-domains.caddy",
            legacy_exists=True,
            authorize_bulk=False,
        )
        assert plan.deletes == ["buckets-custom-domains.caddy"]
        assert plan.rail_tripped is False
        assert set(plan.writes) == {"site-aaaa.caddy", "site-bbbb.caddy"}

    def test_legacy_plus_orphan_over_cadence_trips(self) -> None:
        # Legacy delete plus a managed orphan is two deletes; the rail trips
        # and keeps BOTH (including the legacy) rather than guessing.
        plan = _plan_reconcile(
            tenants={"aaaa": "x\n"},
            on_disk={"site-aaaa.caddy", "site-bbbb.caddy"},
            legacy_name="buckets-custom-domains.caddy",
            legacy_exists=True,
            authorize_bulk=False,
        )
        assert plan.rail_tripped is True
        assert plan.deletes == []
        assert "buckets-custom-domains.caddy" in plan.skipped_deletes
        assert "site-bbbb.caddy" in plan.skipped_deletes


# ---------------------------------------------------------------------------
# The delete rail end to end - the required regression. A suspicious
# mass-delete trips a named failure and leaves the files serving.
# ---------------------------------------------------------------------------


class TestDeleteRailHandler:
    def _seed(self, drop_in_dir: Path, ids: list[str]) -> None:
        drop_in_dir.mkdir()
        for tid in ids:
            (drop_in_dir / f"site-{tid}.caddy").write_text(f"{tid} block\n")

    def test_mass_delete_trips_named_failure_files_keep_serving(
        self, tmp_path: Path,
    ) -> None:
        cfg = _make_config(tmp_path)
        drop_in_dir = cfg.drop_in_path.parent
        # Four sites live on disk.
        self._seed(drop_in_dir, ["aaaa", "bbbb", "cccc", "dddd"])

        mock_conn = _make_mock_connection_sequence(
            (200, "{}"),  # /adapt
            (200, "{}"),  # /load
        )
        with patch(
            "stormpulse.caddy.sync.http.client.HTTPConnection",
            return_value=mock_conn,
        ):
            # Manifest names only one bucket: an under-returning query that
            # would delete the other three.
            handler = make_caddy_sync_handler(
                cfg,
                _params({"aaaa": "aaaa updated\n"}),
            )
            outcome = asyncio.run(_run_handler(handler))

        assert outcome.success is False
        assert outcome.failure_reason == "delete_rail_tripped"
        # The failure names the offending files, the cadence, and the escape
        # hatch so the operator can act on it.
        assert "site-bbbb.caddy" in outcome.stderr
        assert "authorize_bulk" in outcome.stderr
        # The three NOT in the manifest keep serving on disk.
        for tid in ("bbbb", "cccc", "dddd"):
            assert (drop_in_dir / f"site-{tid}.caddy").exists()
        # The write still applied (the safe direction).
        assert (drop_in_dir / "site-aaaa.caddy").read_text() == "aaaa updated\n"
        # Writes were made live: the reload still ran.
        endpoints = [c.args[1] for c in mock_conn.request.call_args_list]
        assert endpoints == ["/adapt", "/load"]

    def test_authorize_bulk_performs_the_mass_delete(
        self, tmp_path: Path,
    ) -> None:
        cfg = _make_config(tmp_path)
        drop_in_dir = cfg.drop_in_path.parent
        self._seed(drop_in_dir, ["aaaa", "bbbb", "cccc", "dddd"])

        mock_conn = _make_mock_connection_sequence((200, "{}"), (200, "{}"))
        with patch(
            "stormpulse.caddy.sync.http.client.HTTPConnection",
            return_value=mock_conn,
        ):
            handler = make_caddy_sync_handler(
                cfg,
                _params({"aaaa": "aaaa\n"}, authorize_bulk=True),
            )
            outcome = asyncio.run(_run_handler(handler))

        assert outcome.success is True
        assert outcome.extras["deleted"] == 3
        for tid in ("bbbb", "cccc", "dddd"):
            assert not (drop_in_dir / f"site-{tid}.caddy").exists()
        assert (drop_in_dir / "site-aaaa.caddy").exists()


# ---------------------------------------------------------------------------
# Cutover from the legacy single-file drop-in to per-bucket files.
# ---------------------------------------------------------------------------


class TestLegacyCutover:
    def test_legacy_monolith_removed_in_same_sync(self, tmp_path: Path) -> None:
        """The first new-shape sync writes per-bucket files AND removes the
        legacy single-file drop-in in the same reconcile, before /adapt, so
        Caddy never adapts a superset where the monolith and a new per-bucket
        file declare the same site (a duplicate-site-address failure)."""
        cfg = _make_config(tmp_path)
        drop_in_dir = cfg.drop_in_path.parent
        drop_in_dir.mkdir()
        # The legacy monolith lives at the configured drop_in_path.
        cfg.drop_in_path.write_text("mathew.stormsites.ca { }\n")
        assert cfg.drop_in_path.name == "buckets-custom-domains.caddy"

        mock_conn = _make_mock_connection_sequence((200, "{}"), (200, "{}"))
        with patch(
            "stormpulse.caddy.sync.http.client.HTTPConnection",
            return_value=mock_conn,
        ):
            handler = make_caddy_sync_handler(
                cfg,
                _params({"aaaa": "site-a\n", "bbbb": "site-b\n"}),
            )
            outcome = asyncio.run(_run_handler(handler))

        assert outcome.success is True
        # Legacy monolith gone; per-bucket files present.
        assert not cfg.drop_in_path.exists()
        assert (drop_in_dir / "site-aaaa.caddy").exists()
        assert (drop_in_dir / "site-bbbb.caddy").exists()
        assert outcome.extras["deleted"] == 1
        # Disk was fully reconciled before the reload.
        endpoints = [c.args[1] for c in mock_conn.request.call_args_list]
        assert endpoints == ["/adapt", "/load"]


# ---------------------------------------------------------------------------
# _read_and_absolutize_imports - pure string transform, no mocks
# ---------------------------------------------------------------------------


class TestReadAndAbsolutizeImports:
    def test_absolute_imports_unchanged(self, tmp_path: Path) -> None:
        path = tmp_path / "Caddyfile"
        path.write_text("import /etc/caddy/conf.d/*.caddy\n")
        result = _read_and_absolutize_imports(path)
        assert result == "import /etc/caddy/conf.d/*.caddy\n"

    def test_relative_imports_absolutized(self, tmp_path: Path) -> None:
        path = tmp_path / "Caddyfile"
        path.write_text("import conf.d/*.caddy\n")
        result = _read_and_absolutize_imports(path)
        assert result == f"import {tmp_path.as_posix()}/conf.d/*.caddy\n"

    def test_comments_unchanged(self, tmp_path: Path) -> None:
        path = tmp_path / "Caddyfile"
        content = "# import this.caddy\n# import that.caddy\n"
        path.write_text(content)
        result = _read_and_absolutize_imports(path)
        assert result == content

    def test_non_import_lines_unchanged(self, tmp_path: Path) -> None:
        path = tmp_path / "Caddyfile"
        content = (
            "{\n    admin localhost:2019\n}\n\nexample.com {\n    respond \"ok\"\n}\n"
        )
        path.write_text(content)
        result = _read_and_absolutize_imports(path)
        assert result == content

    def test_snippet_import_not_absolutized(self, tmp_path: Path) -> None:
        # Regression, 2026-06-11: `import security-headers` references
        # the `(security-headers)` snippet, not a file. Absolutising it
        # made Caddy 400 every /load against the hardened prod
        # Caddyfile ("File to import not found"), so fragments landed
        # on disk but never served until an operator restart.
        path = tmp_path / "Caddyfile"
        path.write_text(
            "(security-headers) {\n"
            '    header X-Frame-Options "DENY"\n'
            "}\n"
            "import conf.d/*.caddy\n"
            ":80 {\n"
            "    import security-headers\n"
            "}\n"
        )
        result = _read_and_absolutize_imports(path)
        assert "import security-headers\n" in result
        assert "/security-headers" not in result
        # The genuine file import is still absolutised.
        assert f"import {tmp_path.as_posix()}/conf.d/*.caddy\n" in result

    def test_relative_file_matching_no_snippet_still_absolutized(
        self, tmp_path: Path,
    ) -> None:
        # A bare relative name with no matching snippet definition is a
        # file path and still gets absolutised.
        path = tmp_path / "Caddyfile"
        path.write_text("import extra-conf\n")
        result = _read_and_absolutize_imports(path)
        assert result == f"import {tmp_path.as_posix()}/extra-conf\n"

    def test_mixed_absolute_and_relative(self, tmp_path: Path) -> None:
        path = tmp_path / "Caddyfile"
        path.write_text(
            "import /etc/caddy/global.caddy\nimport conf.d/*.caddy\nrespond \"hi\"\n"
        )
        result = _read_and_absolutize_imports(path)
        expected = (
            "import /etc/caddy/global.caddy\n"
            f"import {tmp_path.as_posix()}/conf.d/*.caddy\n"
            'respond "hi"\n'
        )
        assert result == expected

    def test_empty_file(self, tmp_path: Path) -> None:
        path = tmp_path / "Caddyfile"
        path.write_text("")
        result = _read_and_absolutize_imports(path)
        assert result == ""


# ---------------------------------------------------------------------------
# Integration: real http.server in a thread.
# Belt-and-suspenders on the mocked tests above - confirms URL routing,
# content-type, and body encoding survive a real socket round-trip.
# ---------------------------------------------------------------------------


class _CaddyAdminStub(BaseHTTPRequestHandler):
    """Tiny HTTP server stub that mimics Caddy admin's /load endpoint."""

    captured: dict[str, Any] = {}

    def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
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
