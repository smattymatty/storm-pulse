"""Tests for the ``stormpulse caddy init`` subcommand orchestration.

Mirrors the structure of ``tests/garage/test_garage_init.py``: small
unit tests around each helper, then a few end-to-end orchestrator
tests with mocked input/root/restart.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from stormpulse.caddy.init import (
    _CADDY_MAIN_SEARCH_PATHS,
    _CADDY_TOML_TEMPLATE,
    append_caddy_section,
    find_caddy_main,
    has_caddy_section,
    remove_caddy_section,
    run_caddy_init,
)
from stormpulse.init import InitError


# ---------------------------------------------------------------------------
# find_caddy_main
# ---------------------------------------------------------------------------


class TestFindCaddyMain:
    def test_override_hits_existing_file(self, tmp_path: Path) -> None:
        cfg = tmp_path / "Caddyfile"
        cfg.write_text("{ }")
        assert find_caddy_main(str(cfg)) == cfg

    def test_override_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert find_caddy_main(str(tmp_path / "nope")) is None

    def test_no_override_no_paths_returns_none(self, tmp_path: Path) -> None:
        # Patch all search paths to point into tmp_path (which contains
        # no Caddyfile) so the test doesn't depend on host filesystem.
        with patch(
            "stormpulse.caddy.init._CADDY_MAIN_SEARCH_PATHS",
            [tmp_path / p.name for p in _CADDY_MAIN_SEARCH_PATHS],
        ):
            assert find_caddy_main() is None

    def test_picks_first_existing_search_path(self, tmp_path: Path) -> None:
        # Create two candidate files and verify search-order preference.
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.write_text("first")
        b.write_text("second")
        with patch(
            "stormpulse.caddy.init._CADDY_MAIN_SEARCH_PATHS",
            [a, b],
        ):
            assert find_caddy_main() == a
        with patch(
            "stormpulse.caddy.init._CADDY_MAIN_SEARCH_PATHS",
            [b, a],
        ):
            assert find_caddy_main() == b


# ---------------------------------------------------------------------------
# TOML section helpers
# ---------------------------------------------------------------------------


class TestHasCaddySection:
    def test_returns_false_when_absent(self, tmp_path: Path) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text('[agent]\nid = "x"\n')
        assert has_caddy_section(cfg) is False

    def test_returns_true_when_present(self, tmp_path: Path) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text('[agent]\nid = "x"\n\n[caddy]\nenabled = true\n')
        assert has_caddy_section(cfg) is True

    def test_missing_file_returns_false(self, tmp_path: Path) -> None:
        assert has_caddy_section(tmp_path / "missing") is False


class TestRemoveCaddySection:
    def test_removes_section_at_end_of_file(self) -> None:
        lines = [
            "[agent]\n",
            'id = "x"\n',
            "\n",
            "[caddy]\n",
            "enabled = true\n",
            'admin_url = "http://localhost:2019"\n',
        ]
        out = remove_caddy_section(lines)
        assert "[caddy]\n" not in out
        assert 'enabled = true\n' not in out
        # The preceding blank line was eaten — no stacked blanks.
        assert out[-1] == 'id = "x"\n'

    def test_removes_section_in_middle(self) -> None:
        lines = [
            "[agent]\n",
            'id = "x"\n',
            "\n",
            "[caddy]\n",
            "enabled = true\n",
            "\n",
            "[storage]\n",
            'db_path = "/x"\n',
        ]
        out = remove_caddy_section(lines)
        # [storage] survives, [caddy] is gone.
        assert "[storage]\n" in out
        assert "[caddy]\n" not in out
        assert "enabled = true\n" not in out

    def test_no_section_returns_unchanged(self) -> None:
        lines = ["[agent]\n", 'id = "x"\n']
        assert remove_caddy_section(lines) == lines


class TestAppendCaddySection:
    def test_writes_block_to_file(self, tmp_path: Path) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text('[agent]\nid = "x"\n')
        append_caddy_section(
            cfg,
            admin_url="http://localhost:2019",
            main_caddyfile="/etc/caddy/Caddyfile",
            drop_in_path="/etc/caddy/conf.d/cellar.caddy",
        )
        content = cfg.read_text()
        assert "[caddy]" in content
        assert 'admin_url = "http://localhost:2019"' in content
        assert 'main_caddyfile = "/etc/caddy/Caddyfile"' in content
        assert 'drop_in_path = "/etc/caddy/conf.d/cellar.caddy"' in content

    def test_blocks_if_section_exists_without_force(self, tmp_path: Path) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text('[caddy]\nenabled = true\n')
        with pytest.raises(InitError, match="already exists"):
            append_caddy_section(
                cfg,
                admin_url="http://localhost:2019",
                main_caddyfile="/x",
                drop_in_path="/y",
            )

    def test_force_replaces_existing_section(self, tmp_path: Path) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text(
            '[agent]\nid = "x"\n\n'
            '[caddy]\nenabled = true\nadmin_url = "http://old:2019"\n'
        )
        append_caddy_section(
            cfg,
            admin_url="http://new:2019",
            main_caddyfile="/etc/caddy/Caddyfile",
            drop_in_path="/etc/caddy/conf.d/cellar.caddy",
            force=True,
        )
        content = cfg.read_text()
        # Old value gone, new value present.
        assert 'admin_url = "http://old:2019"' not in content
        assert 'admin_url = "http://new:2019"' in content
        # Agent section untouched.
        assert '[agent]' in content

    def test_missing_target_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(InitError, match="not found"):
            append_caddy_section(
                tmp_path / "missing",
                admin_url="http://x",
                main_caddyfile="/x",
                drop_in_path="/y",
            )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class TestRunCaddyInit:
    def test_not_root_exits(self, tmp_path: Path) -> None:
        with patch("stormpulse.caddy.init.os.geteuid", return_value=1000):
            with pytest.raises(InitError, match="root"):
                run_caddy_init(tmp_path / "stormpulse.toml")

    def test_no_caddy_detected_raises(self, tmp_path: Path) -> None:
        with patch("stormpulse.caddy.init.os.geteuid", return_value=0):
            with patch(
                "stormpulse.caddy.init.find_caddy_main",
                return_value=None,
            ):
                with pytest.raises(InitError, match="No Caddy installation"):
                    run_caddy_init(tmp_path / "stormpulse.toml")

    def test_section_already_present_without_force_raises(
        self, tmp_path: Path,
    ) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text('[caddy]\nenabled = true\n')
        caddyfile = tmp_path / "Caddyfile"
        caddyfile.write_text("import conf.d/*.caddy\n")
        with patch("stormpulse.caddy.init.os.geteuid", return_value=0):
            with patch(
                "stormpulse.caddy.init.find_caddy_main",
                return_value=caddyfile,
            ):
                with pytest.raises(InitError, match="already exists"):
                    run_caddy_init(cfg)

    @staticmethod
    def _prompt_mock(responses: list[str]):
        """Build a mock _prompt that respects the ``default=`` kwarg.

        The real ``_prompt`` returns the default when the user presses
        enter on an empty line; a naive ``side_effect`` doesn't, which
        would exhaust the iterator inside validation loops that retry
        on empty input.
        """
        it = iter(responses)

        def mock(label: str, default: str | None = None) -> str:
            response = next(it)
            return response if response else (default or "")
        return mock

    def test_happy_path_writes_section_and_offers_restart(
        self, tmp_path: Path,
    ) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text('[agent]\nid = "x"\n')
        # Main Caddyfile that imports the conf.d glob — passes the
        # verify_drop_in_imported check so the orchestrator takes the
        # "Enable Caddy integration?" path, not the warn-and-bail path.
        caddyfile = tmp_path / "Caddyfile"
        caddyfile.write_text("import conf.d/*.caddy\n")

        # Prompts in order: admin URL, drop-in path, confirm, restart.
        # Empty strings accept defaults; "n" declines restart.
        with patch("stormpulse.caddy.init.os.geteuid", return_value=0):
            with patch(
                "stormpulse.caddy.init.find_caddy_main",
                return_value=caddyfile,
            ):
                with patch(
                    "stormpulse.caddy.init._prompt",
                    side_effect=self._prompt_mock(["", "", "y", "n"]),
                ):
                    run_caddy_init(cfg)

        content = cfg.read_text()
        assert "[caddy]" in content
        assert 'admin_url = "http://localhost:2019"' in content
        assert f'main_caddyfile = "{caddyfile}"' in content
        # Default drop-in derived from main Caddyfile's parent.
        assert 'drop_in_path = "' + str(
            tmp_path / "conf.d" / "cellar-custom-domains.caddy"
        ) + '"' in content

    def test_missing_import_directive_warns_and_can_abort(
        self, tmp_path: Path,
    ) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text('[agent]\nid = "x"\n')
        # Main Caddyfile WITHOUT any import — verify_drop_in_imported
        # returns an error message, the orchestrator switches to the
        # warn-with-default-no path. We answer 'n' to "write anyway?"
        # to confirm the abort.
        caddyfile = tmp_path / "Caddyfile"
        caddyfile.write_text(":80 { respond \"ok\" }\n")

        with patch("stormpulse.caddy.init.os.geteuid", return_value=0):
            with patch(
                "stormpulse.caddy.init.find_caddy_main",
                return_value=caddyfile,
            ):
                with patch(
                    "stormpulse.caddy.init._prompt",
                    side_effect=self._prompt_mock(["", "", "n"]),
                ):
                    run_caddy_init(cfg)

        # The [caddy] section was NOT written.
        assert "[caddy]" not in cfg.read_text()

    def test_missing_import_directive_write_anyway_skips_restart(
        self, tmp_path: Path,
    ) -> None:
        cfg = tmp_path / "stormpulse.toml"
        cfg.write_text('[agent]\nid = "x"\n')
        caddyfile = tmp_path / "Caddyfile"
        caddyfile.write_text(":80 { respond \"ok\" }\n")

        # admin URL, drop-in, write-anyway=y. No restart prompt because
        # the import-missing branch suppresses restart entirely.
        with patch("stormpulse.caddy.init.os.geteuid", return_value=0):
            with patch(
                "stormpulse.caddy.init.find_caddy_main",
                return_value=caddyfile,
            ):
                with patch(
                    "stormpulse.caddy.init._prompt",
                    side_effect=self._prompt_mock(["", "", "y"]),
                ):
                    with patch(
                        "stormpulse.caddy.init.restart_stormpulse",
                    ) as mock_restart:
                        run_caddy_init(cfg)

        # Section written but restart NEVER called — fix-import-first
        # path skips the restart prompt entirely.
        assert "[caddy]" in cfg.read_text()
        mock_restart.assert_not_called()


# ---------------------------------------------------------------------------
# TOML template smoke check
# ---------------------------------------------------------------------------


class TestCaddyTomlTemplate:
    def test_template_renders_valid_toml(self) -> None:
        # Confirm the template doesn't drift into invalid TOML.
        import tomllib
        rendered = _CADDY_TOML_TEMPLATE.format(
            admin_url="http://localhost:2019",
            main_caddyfile="/etc/caddy/Caddyfile",
            drop_in_path="/etc/caddy/conf.d/cellar.caddy",
        )
        parsed = tomllib.loads(rendered)
        assert parsed["caddy"]["enabled"] is True
        assert parsed["caddy"]["admin_url"] == "http://localhost:2019"
