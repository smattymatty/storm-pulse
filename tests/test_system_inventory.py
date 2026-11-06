"""The register's OS block, read from a real os-release file."""

from __future__ import annotations

from pathlib import Path

import pytest

from stormpulse import system_inventory


def _inventory_from(
    os_release: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, object]:
    path = tmp_path / "os-release"
    path.write_text(os_release, encoding="utf-8")
    monkeypatch.setattr(system_inventory, "_OS_RELEASE", path)
    monkeypatch.setattr(system_inventory, "_SERVICE_PROBES", [])
    return system_inventory.collect_system_inventory()


def test_os_block_carries_the_codename_an_advisory_feed_is_keyed_by(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = _inventory_from(
        'ID=debian\nVERSION_ID="12"\nVERSION_CODENAME=bookworm\n'
        'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\n',
        tmp_path,
        monkeypatch,
    )
    assert inventory["os"] == {
        "id": "debian",
        "version": "12",
        "codename": "bookworm",
        "pretty": "Debian GNU/Linux 12 (bookworm)",
    }


def test_os_block_omits_the_codename_where_os_release_has_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = _inventory_from(
        "ID=arch\nPRETTY_NAME=\"Arch Linux\"\n", tmp_path, monkeypatch
    )
    assert inventory["os"] == {"id": "arch", "pretty": "Arch Linux"}


# Fixture rows in the agent's --showformat; not checked against a live dpkg-query.
_DPKG = (
    "ii \tlibsqlite3-0:amd64\t3.40.1-2+deb12u1\tsqlite3\t3.40.1-2+deb12u1\n"
    "ii \tsqlite3\t3.40.1-2+deb12u1\tsqlite3\t3.40.1-2+deb12u1\n"
    "rc \tlibssl1.1:amd64\t1.1.1n-0+deb11u5\topenssl\t1.1.1n-0+deb11u5\n"
    "hi \tcaddy\t2.7.6\tcaddy\t2.7.6\n"
    "ii \tlinux-image-6.1.0-25-amd64\t6.1.106-3\tlinux-signed-amd64\t6.1.106+3\n"
    "ii \tlinux-image-6.1.0-26-amd64\t6.1.112-1\tlinux-signed-amd64\t6.1.112+1\n"
    "ii \tlibGLU1-mesa:amd64\t9.0.2-1.1\tLibGLU\t9.0.2-1.1\n"
)


def test_fold_sends_each_installed_source_at_its_source_version() -> None:
    folded = system_inventory.fold_dpkg_rows(_DPKG)
    assert folded is not None
    assert [(p.name, p.version) for p in folded] == [
        ("caddy", "2.7.6"),
        ("libglu", "9.0.2-1.1"),
        ("linux-signed-amd64", "6.1.106+3"),
        ("linux-signed-amd64", "6.1.112+1"),
        ("sqlite3", "3.40.1-2+deb12u1"),
    ]


@pytest.mark.parametrize("abbrev", ["un ", "pn ", "in "])
def test_a_known_but_not_installed_package_is_not_reported(abbrev: str) -> None:
    """Only status ``i`` is installed; a purged or never-installed row that
    dpkg still knows must not reach the advisory match."""
    rows = _DPKG + f"{abbrev}\tlibc6:amd64\t2.36-9\tglibc\t2.36-9\n"
    folded = system_inventory.fold_dpkg_rows(rows)
    assert folded is not None
    assert "glibc" not in [p.name for p in folded]


@pytest.mark.parametrize("abbrev", ["iU ", "iH ", "iF ", "iW ", "it ", "iiR"])
def test_one_unsettled_row_fails_the_whole_collect(abbrev: str) -> None:
    rows = _DPKG + f"{abbrev}\tlibc6:amd64\t2.36-9\tglibc\t2.36-9\n"
    assert system_inventory.fold_dpkg_rows(rows) is None


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param("", id="empty"),
        pytest.param("rc \tx\t1\tx\t1\n", id="nothing installed"),
        pytest.param(_DPKG + "ii \tlibc6\t2.36-9\tglibc\n", id="short row"),
        pytest.param(_DPKG + "ii\tlibc6\t2.36-9\tglibc\t2.36-9\n", id="short status"),
    ],
)
def test_nothing_to_send_rather_than_a_list_the_receiver_would_prune(
    rows: str,
) -> None:
    assert system_inventory.fold_dpkg_rows(rows) is None


def _dpkg_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: str, exit_code: int = 0
) -> Path:
    """A bookworm os-release and a stand-in dpkg-query, the only one on PATH."""
    (tmp_path / "os-release").write_text("VERSION_CODENAME=bookworm\n")
    (tmp_path / "rows").write_text(rows)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tool = bindir / "dpkg-query"
    tool.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > {tmp_path}/argv\n'
        f"/bin/cat {tmp_path}/rows\nexit {exit_code}\n"
    )
    tool.chmod(0o755)
    monkeypatch.setattr(system_inventory, "_OS_RELEASE", tmp_path / "os-release")
    monkeypatch.setenv("PATH", str(bindir))
    return tmp_path / "argv"


def test_collect_runs_dpkg_query_with_the_format_as_one_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = _dpkg_host(tmp_path, monkeypatch, _DPKG)
    payload = system_inventory.collect_source_packages()
    assert payload is not None
    assert payload.release == "bookworm"
    assert ("sqlite3", "3.40.1-2+deb12u1") in [
        (p.name, p.version) for p in payload.packages
    ]
    assert argv.read_text() == (
        "-W\n--showformat=${db:Status-Abbrev}\t${Package}\t${Version}"
        "\t${source:Package}\t${source:Version}\n\n"
    )


def test_collect_sends_nothing_when_dpkg_query_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dpkg_host(tmp_path, monkeypatch, _DPKG, exit_code=2)
    assert system_inventory.collect_source_packages() is None


def test_collect_sends_nothing_without_a_codename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dpkg_host(tmp_path, monkeypatch, _DPKG)
    (tmp_path / "os-release").write_text("ID=debian\n")
    assert system_inventory.collect_source_packages() is None


def test_a_host_without_dpkg_query_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _dpkg_host(tmp_path, monkeypatch, _DPKG)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert system_inventory.collect_source_packages() is None
