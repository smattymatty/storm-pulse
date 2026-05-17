"""Tests for the boot-time Caddyfile import verifier."""

from __future__ import annotations

from pathlib import Path

import pytest

from stormpulse.caddy.sync import verify_drop_in_imported


@pytest.fixture
def caddy_root(tmp_path: Path) -> Path:
    """Mimic a typical /etc/caddy layout: Caddyfile + conf.d/ subdir."""
    (tmp_path / "conf.d").mkdir()
    return tmp_path


def _write_main(caddy_root: Path, content: str) -> Path:
    main = caddy_root / "Caddyfile"
    main.write_text(content)
    return main


class TestVerifyDropInImported:
    def test_exact_absolute_import_matches(self, caddy_root: Path) -> None:
        drop_in = caddy_root / "conf.d" / "cellar-custom-domains.caddy"
        main = _write_main(
            caddy_root,
            f"{{\n\tadmin localhost:2019\n}}\n\nimport {drop_in}\n",
        )
        assert verify_drop_in_imported(main, drop_in) is None

    def test_relative_import_matches(self, caddy_root: Path) -> None:
        drop_in = caddy_root / "conf.d" / "cellar-custom-domains.caddy"
        main = _write_main(
            caddy_root,
            "import conf.d/cellar-custom-domains.caddy\n",
        )
        assert verify_drop_in_imported(main, drop_in) is None

    def test_glob_import_matches(self, caddy_root: Path) -> None:
        drop_in = caddy_root / "conf.d" / "cellar-custom-domains.caddy"
        main = _write_main(caddy_root, "import conf.d/*.caddy\n")
        assert verify_drop_in_imported(main, drop_in) is None

    def test_glob_absolute_matches(self, caddy_root: Path) -> None:
        drop_in = caddy_root / "conf.d" / "cellar-custom-domains.caddy"
        main = _write_main(caddy_root, f"import {caddy_root}/conf.d/*\n")
        assert verify_drop_in_imported(main, drop_in) is None

    def test_drop_in_file_does_not_need_to_exist(self, caddy_root: Path) -> None:
        # The boot check must work BEFORE the first sync writes the file.
        drop_in = caddy_root / "conf.d" / "not-yet.caddy"
        assert not drop_in.exists()
        main = _write_main(caddy_root, "import conf.d/*.caddy\n")
        assert verify_drop_in_imported(main, drop_in) is None

    def test_no_import_returns_error(self, caddy_root: Path) -> None:
        drop_in = caddy_root / "conf.d" / "cellar-custom-domains.caddy"
        main = _write_main(caddy_root, ":80 {\n\trespond \"ok\"\n}\n")
        err = verify_drop_in_imported(main, drop_in)
        assert err is not None
        assert "does not import" in err

    def test_unrelated_import_returns_error(self, caddy_root: Path) -> None:
        drop_in = caddy_root / "conf.d" / "cellar-custom-domains.caddy"
        main = _write_main(caddy_root, "import other.caddy\n")
        err = verify_drop_in_imported(main, drop_in)
        assert err is not None

    def test_comment_lines_ignored(self, caddy_root: Path) -> None:
        # Commented-out import does not count as configured.
        drop_in = caddy_root / "conf.d" / "cellar-custom-domains.caddy"
        main = _write_main(
            caddy_root,
            f"# import {drop_in}\n:80 {{ respond \"ok\" }}\n",
        )
        err = verify_drop_in_imported(main, drop_in)
        assert err is not None

    def test_inline_comments_stripped(self, caddy_root: Path) -> None:
        drop_in = caddy_root / "conf.d" / "cellar-custom-domains.caddy"
        main = _write_main(
            caddy_root,
            "import conf.d/*.caddy  # auto-managed by Pulse\n",
        )
        assert verify_drop_in_imported(main, drop_in) is None

    def test_missing_main_caddyfile_returns_error(self, caddy_root: Path) -> None:
        drop_in = caddy_root / "conf.d" / "cellar-custom-domains.caddy"
        missing_main = caddy_root / "does-not-exist"
        err = verify_drop_in_imported(missing_main, drop_in)
        assert err is not None
        assert "not found" in err

    def test_glob_pattern_does_not_match_wrong_directory(self, caddy_root: Path) -> None:
        # import conf.d/*.caddy must NOT match a drop-in in a different dir.
        drop_in = caddy_root / "elsewhere" / "cellar-custom-domains.caddy"
        drop_in.parent.mkdir()
        main = _write_main(caddy_root, "import conf.d/*.caddy\n")
        err = verify_drop_in_imported(main, drop_in)
        assert err is not None
