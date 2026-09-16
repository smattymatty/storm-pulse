"""CORE-009 decision 11: authoring a deploy subject from a unit file.

The interactive shell is thin on purpose, so these tests pin the decisions it
makes rather than its prompts: which units get offered, what happens when a unit
says nothing, and that a proposal is written whole or not at all.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from stormpulse.cli import deploy_subject_init as dsi
from stormpulse.wizard.deploy_subject import parse_unit_properties

_UNIT_SHOW = (
    "FragmentPath=/etc/systemd/system/storm-buckets-guard.service\n"
    "WorkingDirectory=/home/storm/guard\n"
    "ExecStart=/home/storm/guard/storm-buckets-guard --serve\n"
)


def _config(tmp_path: Path, body: str = "") -> Path:
    p = tmp_path / "stormpulse.toml"
    p.write_text(body)
    return p


class TestCandidateUnits:
    def test_a_unit_that_already_has_a_subject_is_not_offered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Offering it again would invite a second table for one subject, and
        the merge has no rule for two node-side tables."""
        monkeypatch.setattr(
            dsi, "detect_candidate_units",
            lambda _c: ["storm-buckets-guard.service", "other.service"],
        )
        cfg = _config(tmp_path, """
[investigate.deploy.storm-buckets-guard]
units = ["storm-buckets-guard.service"]
expected_root = "/home/storm/guard"
search_roots = ["/home/storm"]
""")
        assert dsi.candidate_units(cfg) == ["other.service"]

    def test_an_unreadable_config_still_offers_units(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Authoring must work on the box where config is broken; that is when
        the operator most needs to fix it."""
        monkeypatch.setattr(dsi, "detect_candidate_units", lambda _c: ["a.service"])
        cfg = _config(tmp_path, "this is not toml [[[")
        assert dsi.candidate_units(cfg) == ["a.service"]


class TestRunInit:
    def _drive(self, monkeypatch, *, units, show, answer, confirm=True):
        monkeypatch.setattr(dsi, "detect_candidate_units", lambda _c: units)
        monkeypatch.setattr(dsi, "_show_unit", lambda _u: show)
        monkeypatch.setattr(dsi, "prompt", lambda *_a, **_k: answer)
        monkeypatch.setattr(dsi, "prompt_confirm", lambda *_a, **_k: confirm)

    def test_a_derived_subject_is_written_and_parses_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import tomllib

        from stormpulse.config import _deploy_probe_tables, merge_deploy_probes
        cfg = _config(tmp_path)
        self._drive(
            monkeypatch,
            units=["storm-buckets-guard.service"],
            show=parse_unit_properties(_UNIT_SHOW),
            answer="1",
        )
        assert dsi.run_init(cfg) == 0
        # The round trip is the point: a section this writes must be one the
        # loader accepts, or authoring produces a file that fails on next run.
        raw = tomllib.loads(cfg.read_text())
        probes = merge_deploy_probes(_deploy_probe_tables(raw))
        assert probes["storm-buckets-guard"].expected_root == Path("/home/storm/guard")

    def test_a_unit_that_says_nothing_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = _config(tmp_path)
        self._drive(monkeypatch, units=["mystery.service"], show={}, answer="1")
        assert dsi.run_init(cfg) == 1
        assert cfg.read_text() == "", "a refusal must not leave a partial table"

    def test_declining_the_confirm_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = _config(tmp_path)
        self._drive(
            monkeypatch,
            units=["storm-buckets-guard.service"],
            show=parse_unit_properties(_UNIT_SHOW),
            answer="1",
            confirm=False,
        )
        assert dsi.run_init(cfg) == 0
        assert cfg.read_text() == ""

    def test_a_pick_outside_the_offered_range_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = _config(tmp_path)
        self._drive(monkeypatch, units=["a.service"], show={}, answer="7")
        assert dsi.run_init(cfg) == 1
        assert cfg.read_text() == ""

    def test_no_candidates_is_a_normal_answer_not_a_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = _config(tmp_path)
        self._drive(monkeypatch, units=[], show={}, answer="")
        assert dsi.run_init(cfg) == 0
