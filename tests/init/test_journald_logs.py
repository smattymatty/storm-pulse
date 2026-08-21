"""Tests for the journald log_group offer.

The behaviour worth defending is the one a hand-edited TOML block cannot have:
the wizard reads the unit's journal before writing, so a typo is caught at
setup instead of becoming a group that ships nothing forever. And the block it
writes must be one the config loader will actually accept, because a block
skipped at load leaves the operator believing they configured something.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from stormpulse.config import load_config
from stormpulse.init.journald_logs import (
    group_name_for_unit,
    offer_journald_log_groups,
    probe_unit_journal,
)


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    p = tmp_path / "stormpulse.toml"
    p.write_text("# baseline\n")
    return p


def _ok(stdout: str = "Aug 21 10:00:00 host svc[1]: up\n"):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _fail(stderr: str = "Failed to add match: Invalid argument", rc: int = 1):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout="", stderr=stderr)


def _answers(*values):
    """Patch the module's prompts with a scripted sequence."""
    confirms = [v for v in values if isinstance(v, bool)]
    texts = [v for v in values if isinstance(v, str)]
    return (
        patch("stormpulse.init.journald_logs.prompt_confirm", side_effect=confirms),
        patch("stormpulse.init.journald_logs.prompt", side_effect=texts),
    )


# --- group naming --------------------------------------------------------


def test_group_name_strips_the_unit_suffix() -> None:
    assert group_name_for_unit("my-daemon.service") == "my-daemon"


def test_group_name_replaces_characters_the_loader_would_reject() -> None:
    """config._LOG_NAME_PATTERN allows alphanumeric, underscore and hyphen
    only. Writing a name it refuses produces a block skipped at load, which
    looks exactly like a configured group that ships nothing."""
    assert group_name_for_unit("foo.bar.service") == "foo-bar"
    assert group_name_for_unit("weird@name.service") == "weird-name"


def test_group_name_is_never_empty() -> None:
    assert group_name_for_unit(".service") == "unit"


# --- probing -------------------------------------------------------------


def test_probe_accepts_a_unit_with_entries() -> None:
    with patch("stormpulse.init.journald_logs.subprocess.run", return_value=_ok()):
        assert probe_unit_journal("my-daemon.service") is None


def test_probe_reports_a_unit_journalctl_refuses() -> None:
    with patch("stormpulse.init.journald_logs.subprocess.run", return_value=_fail()):
        assert "Invalid argument" in (probe_unit_journal("nope.service") or "")


def test_probe_distinguishes_an_empty_journal_from_a_failure() -> None:
    """A freshly installed service has written nothing. That must not be
    reported the same way a bad unit name is, because refusing it would be
    worse than the typo this guards against."""
    with patch("stormpulse.init.journald_logs.subprocess.run", return_value=_ok("")):
        assert probe_unit_journal("fresh.service") == "no journal entries yet"


def test_probe_survives_a_missing_journalctl() -> None:
    with patch(
        "stormpulse.init.journald_logs.subprocess.run", side_effect=FileNotFoundError,
    ):
        assert probe_unit_journal("x.service") == "journalctl is not installed"


# --- the offer -----------------------------------------------------------


def test_a_box_without_journalctl_is_never_asked(cfg: Path) -> None:
    with patch("stormpulse.init.journald_logs.journalctl_available", return_value=False):
        with patch("stormpulse.init.journald_logs.prompt_confirm") as confirm:
            assert offer_journald_log_groups(cfg) is False
    confirm.assert_not_called()


def test_declining_writes_nothing(cfg: Path) -> None:
    confirms, texts = _answers(False)
    with patch("stormpulse.init.journald_logs.journalctl_available", return_value=True), \
            confirms, texts:
        assert offer_journald_log_groups(cfg) is False
    assert cfg.read_text() == "# baseline\n"


def test_a_readable_unit_is_written_and_loads(cfg: Path) -> None:
    """End to end: the block the wizard writes must survive the real loader."""
    confirms, texts = _answers(True, "my-daemon.service", "")
    with patch("stormpulse.init.journald_logs.journalctl_available", return_value=True), \
            patch("stormpulse.init.journald_logs.subprocess.run", return_value=_ok()), \
            confirms, texts:
        assert offer_journald_log_groups(cfg) is True

    raw = tomllib.loads(cfg.read_text())
    assert raw["log_groups"][0]["unit"] == "my-daemon.service"
    assert raw["log_groups"][0]["name"] == "my-daemon"
    assert raw["log_groups"][0]["parser"] == "journald"


def test_an_unreadable_unit_is_refused_unless_confirmed(cfg: Path) -> None:
    confirms, texts = _answers(True, False, "typoed.servcie", "")
    with patch("stormpulse.init.journald_logs.journalctl_available", return_value=True), \
            patch("stormpulse.init.journald_logs.subprocess.run", return_value=_fail()), \
            confirms, texts:
        assert offer_journald_log_groups(cfg) is False
    assert "log_groups" not in tomllib.loads(cfg.read_text())


def test_an_unreadable_unit_can_be_forced(cfg: Path) -> None:
    """The operator may know something the probe cannot: a unit about to be
    installed, or one whose journal this user cannot read but the agent's can."""
    confirms, texts = _answers(True, True, "future.service", "")
    with patch("stormpulse.init.journald_logs.journalctl_available", return_value=True), \
            patch("stormpulse.init.journald_logs.subprocess.run", return_value=_fail()), \
            confirms, texts:
        assert offer_journald_log_groups(cfg) is True
    assert tomllib.loads(cfg.read_text())["log_groups"][0]["unit"] == "future.service"


def test_several_units_in_one_pass(cfg: Path) -> None:
    confirms, texts = _answers(True, "a.service", "b.service", "")
    with patch("stormpulse.init.journald_logs.journalctl_available", return_value=True), \
            patch("stormpulse.init.journald_logs.subprocess.run", return_value=_ok()), \
            confirms, texts:
        assert offer_journald_log_groups(cfg) is True
    names = [g["name"] for g in tomllib.loads(cfg.read_text())["log_groups"]]
    assert names == ["a", "b"]


def test_an_existing_group_name_is_not_duplicated(cfg: Path) -> None:
    """Re-running the wizard must not append a second block: a duplicate name
    is skipped at load, so the second one would silently do nothing."""
    cfg.write_text(
        '[[log_groups]]\nname = "my-daemon"\nenabled = true\n'
        'source_type = "journald"\nunit = "my-daemon.service"\n'
        'parser = "journald"\nship_interval_seconds = 10\n'
        'max_lines_per_batch = 200\n'
    )
    confirms, texts = _answers(True, "my-daemon.service", "")
    with patch("stormpulse.init.journald_logs.journalctl_available", return_value=True), \
            patch("stormpulse.init.journald_logs.subprocess.run", return_value=_ok()), \
            confirms, texts:
        assert offer_journald_log_groups(cfg) is False
    assert len(tomllib.loads(cfg.read_text())["log_groups"]) == 1


def test_a_unit_with_whitespace_is_refused(cfg: Path) -> None:
    """The unit becomes one argv element. Whitespace means the operator meant
    something the agent cannot honour, and the loader refuses it too."""
    confirms, texts = _answers(True, "svc.service --since=yesterday", "")
    with patch("stormpulse.init.journald_logs.journalctl_available", return_value=True), \
            patch("stormpulse.init.journald_logs.subprocess.run", return_value=_ok()), \
            confirms, texts:
        assert offer_journald_log_groups(cfg) is False
    assert "log_groups" not in tomllib.loads(cfg.read_text())


def test_the_written_block_passes_the_real_config_loader(cfg: Path, tmp_path: Path) -> None:
    """The strongest form of the naming test: not "it looks right" but "the
    loader this repo ships accepts it"."""
    from tests.test_config import MINIMAL_VALID

    minimal = (tmp_path / "full.toml")
    minimal.write_text(MINIMAL_VALID)
    confirms, texts = _answers(True, "my-daemon.service", "")
    with patch("stormpulse.init.journald_logs.journalctl_available", return_value=True), \
            patch("stormpulse.init.journald_logs.subprocess.run", return_value=_ok()), \
            confirms, texts:
        offer_journald_log_groups(minimal)

    cfg_obj = load_config(minimal)
    assert len(cfg_obj.log_groups) == 1, "the loader silently skipped the block"
    assert cfg_obj.log_groups[0].unit == "my-daemon.service"
    assert cfg_obj.log_groups[0].source_type == "journald"


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

_UNIT_FILES = """\
storm-buckets-guard.service               enabled  enabled
stormpulse.service                        enabled  enabled
ssh.service                               enabled  enabled
getty@.service                            enabled  enabled
app@.service                              enabled  enabled
restic-backup.service                     static   -
restic-backup.timer                       enabled  enabled
"""

_SHOW = """\
Id=storm-buckets-guard.service
FragmentPath=/etc/systemd/system/storm-buckets-guard.service

Id=stormpulse.service
FragmentPath=/etc/systemd/system/stormpulse.service

Id=ssh.service
FragmentPath=/usr/lib/systemd/system/ssh.service

Id=restic-backup.service
FragmentPath=/etc/systemd/system/restic-backup.service

Id=restic-backup.timer
FragmentPath=/etc/systemd/system/restic-backup.timer

Id=app@.service
FragmentPath=/etc/systemd/system/app@.service
"""


def _systemctl(unit_files: str = _UNIT_FILES, show: str = _SHOW):
    """Stand in for systemctl, dispatching on the subcommand."""
    def run(argv, **kwargs):
        if argv[0] == "journalctl":
            return _ok()
        if argv[1] == "list-unit-files":
            return subprocess.CompletedProcess(argv, 0, stdout=unit_files, stderr="")
        if argv[1] == "show":
            # Real systemctl returns blocks only for the units it was asked
            # about. Returning all of them regardless let a unit that the
            # caller had already filtered out reappear here, which made the
            # template test pass for the wrong reason (caught by mutation).
            asked = {a for a in argv if not a.startswith("--")}
            blocks = [b for b in show.split("\n\n")
                      if any(line[3:] in asked
                             for line in b.splitlines() if line.startswith("Id="))]
            return subprocess.CompletedProcess(
                argv, 0, stdout="\n\n".join(blocks), stderr="",
            )
        raise AssertionError(f"unexpected systemctl call: {argv}")
    return run


def _detect(configured=frozenset(), **kw):
    from stormpulse.init.journald_logs import detect_candidate_units

    with patch("stormpulse.init.journald_logs.subprocess.run", _systemctl(**kw)):
        return detect_candidate_units(set(configured))


def test_distro_units_are_not_offered():
    """The whole filter. ``ssh.service`` lives under /usr/lib because the distro
    put it there, and a box has hundreds like it. Offering them makes the list
    useless, which is the same as having no list."""
    assert "ssh.service" not in _detect()


def test_operator_installed_units_are_offered():
    assert "storm-buckets-guard.service" in _detect()


def test_a_timer_and_its_oneshot_are_offered():
    """The case the feature exists for, and the one a ``--state=running`` filter
    would hide. A timer's oneshot is stopped almost always, and it is precisely
    the unit with no log file and no container to read instead."""
    found = _detect()
    assert "restic-backup.timer" in found
    assert "restic-backup.service" in found


def test_the_agent_never_offers_itself():
    """The agent already ships its own output as structured JSON via
    logging.writer. A journald group on it would ship a second unstructured
    copy, and every "shipped batch N" line it logs would become a line to
    ship."""
    assert "stormpulse.service" not in _detect()


def test_template_units_are_not_offered():
    """``app@.service`` is not addressable as written: journalctl needs a
    concrete instance. Offering it would produce a group matching nothing.

    The fixture deliberately puts it under /etc with a FragmentPath, so the
    operator-installed filter CANNOT be what excludes it. Without that, this
    test passed while the template rule did nothing (caught by mutation)."""
    assert not any("@" in u for u in _detect())


def test_already_configured_units_are_not_offered_again():
    assert "storm-buckets-guard.service" not in _detect(
        configured={"storm-buckets-guard"},
    )


def test_a_box_without_systemd_detects_nothing_and_does_not_raise():
    from stormpulse.init.journald_logs import detect_candidate_units

    with patch("stormpulse.init.journald_logs.subprocess.run",
               side_effect=FileNotFoundError):
        assert detect_candidate_units(set()) == []


def test_a_failing_systemctl_detects_nothing_and_does_not_raise():
    from stormpulse.init.journald_logs import detect_candidate_units

    def boom(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="nope")

    with patch("stormpulse.init.journald_logs.subprocess.run", boom):
        assert detect_candidate_units(set()) == []


# ---------------------------------------------------------------------------
# Detection must not become a cage
# ---------------------------------------------------------------------------

def test_free_text_still_works_when_detection_finds_nothing(cfg: Path):
    """The escape hatch, and the reason the filter is safe to be strict. A
    distro-packaged unit an operator genuinely wants must still have a way in."""
    with patch("stormpulse.init.journald_logs.journalctl_available", return_value=True), \
         patch("stormpulse.init.journald_logs.subprocess.run",
               side_effect=FileNotFoundError), \
         patch("stormpulse.init.journald_logs.prompt_confirm", return_value=True), \
         patch("stormpulse.init.journald_logs.probe_unit_journal", return_value=None), \
         patch("stormpulse.init.journald_logs.prompt",
               side_effect=["ssh.service", ""]):
        assert offer_journald_log_groups(cfg) is True

    raw = tomllib.loads(cfg.read_text())
    assert [g["unit"] for g in raw["log_groups"]] == ["ssh.service"]


def test_picking_from_the_list_writes_the_group(cfg: Path):
    with patch("stormpulse.init.journald_logs.subprocess.run", _systemctl()), \
         patch("stormpulse.init.journald_logs.prompt_confirm", return_value=True), \
         patch("stormpulse.init.journald_logs.probe_unit_journal", return_value=None), \
         patch("stormpulse.init.journald_logs.prompt", side_effect=["1", ""]):
        assert offer_journald_log_groups(cfg) is True

    raw = tomllib.loads(cfg.read_text())
    assert raw["log_groups"][0]["source_type"] == "journald"
    assert raw["log_groups"][0]["unit"] == _detect()[0]


def test_a_picked_unit_is_probed_like_a_typed_one(cfg: Path):
    """Detection says a unit EXISTS, never that this user can read its journal.
    On a box where the agent is not in systemd-journal those are different
    questions, and skipping the probe for picked units would reintroduce the
    silent-empty-group failure for exactly the units we made easiest to add."""
    # [open the offer, then decline "Add it anyway?"]. One return_value would
    # answer the opening question False and the function would never start.
    with patch("stormpulse.init.journald_logs.subprocess.run", _systemctl()), \
         patch("stormpulse.init.journald_logs.prompt_confirm",
               side_effect=[True, False]), \
         patch("stormpulse.init.journald_logs.probe_unit_journal",
               return_value="no journal entries yet") as probe, \
         patch("stormpulse.init.journald_logs.prompt", side_effect=["1", ""]):
        offer_journald_log_groups(cfg)

    probe.assert_called_once()
    assert "log_groups" not in tomllib.loads(cfg.read_text())


def test_a_number_outside_the_list_is_skipped_not_guessed(cfg: Path):
    with patch("stormpulse.init.journald_logs.subprocess.run", _systemctl()), \
         patch("stormpulse.init.journald_logs.prompt_confirm", return_value=True), \
         patch("stormpulse.init.journald_logs.probe_unit_journal", return_value=None), \
         patch("stormpulse.init.journald_logs.prompt", side_effect=["99", ""]):
        assert offer_journald_log_groups(cfg) is False

    assert "log_groups" not in tomllib.loads(cfg.read_text())
