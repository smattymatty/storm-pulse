"""Tests for ``stormpulse investigate deploy`` (ADR CORE-009).

Two halves, and the split is the ADR's fitness-function seam:

- **Judges** are pure functions over fetched text, so every verdict path is
  exercised with string fixtures and no host.
- **The bounded walk** touches a filesystem, but its bounds ARE the refusal
  (decision 4), so they are exercised against a fixture tree that contains a
  match the walk must decline to report.
"""

from __future__ import annotations

from typing import Any

import os
from datetime import datetime
from pathlib import Path

import pytest

from stormpulse.cli.investigate.deploy import (
    Artifact,
    _bounded,
    _report_listeners,
    _report_processes,
    judge_artifacts,
    judge_listeners,
    judge_processes,
    judge_unit_show,
    self_lineage,
    walk_artifacts,
)
from stormpulse.config import ConfigError, _parse_deploy_probes
from stormpulse.sdk import SdkDeploySubject


def _raw(**subject_tables: dict[str, Any]) -> dict[str, Any]:
    return {"investigate": {"deploy": subject_tables}}


_GOOD = {
    "units": ["storm-buckets-guard.service"],
    "expected_root": "/home/storm/guard",
    "search_roots": ["/home/storm", "/opt/storm"],
    "ports": [6188],
    "max_depth": 3,
}


class TestDeployProbeConfig:
    def test_valid_subject_parses(self) -> None:
        probes = _parse_deploy_probes(_raw(**{"storm-buckets-guard": _GOOD}))
        probe = probes["storm-buckets-guard"]
        assert probe.units == ("storm-buckets-guard.service",)
        assert probe.expected_root == Path("/home/storm/guard")
        assert probe.search_roots == (Path("/home/storm"), Path("/opt/storm"))
        assert probe.ports == (6188,)
        assert probe.max_depth == 3
        assert probe.max_bytes == 65_536

    def test_expected_root_outside_every_search_root_is_refused(self) -> None:
        # The probe could never look at the place the unit installs, so every
        # verdict it reached would be about somewhere else.
        bad = {**_GOOD, "expected_root": "/srv/guard"}
        assert _parse_deploy_probes(_raw(g=bad)) == {}

    def test_relative_path_is_refused(self) -> None:
        # Relative root AND a relative expected_root inside it: containment
        # holds, so only the absolute-path refusal can reject this.
        bad = {
            **_GOOD,
            "expected_root": "home/storm/guard",
            "search_roots": ["home/storm"],
        }
        assert _parse_deploy_probes(_raw(g=bad)) == {}

    def test_traversal_segment_is_refused(self) -> None:
        # "/home/storm/.." is / wearing a costume. The pair below passes the
        # containment check on pure paths, so the '..' refusal is the only
        # thing that can reject it.
        bad = {
            **_GOOD,
            "expected_root": "/home/storm/../etc/guard",
            "search_roots": ["/home/storm/../etc"],
        }
        assert _parse_deploy_probes(_raw(g=bad)) == {}

    def test_unit_name_that_is_a_path_is_refused(self) -> None:
        bad = {**_GOOD, "units": ["/etc/systemd/system/guard.service"]}
        assert _parse_deploy_probes(_raw(g=bad)) == {}

    def test_depth_above_the_ceiling_is_refused(self) -> None:
        assert _parse_deploy_probes(_raw(g={**_GOOD, "max_depth": 9})) == {}

    def test_depth_at_the_ceiling_is_kept(self) -> None:
        probes = _parse_deploy_probes(_raw(g={**_GOOD, "max_depth": 8}))
        assert probes["g"].max_depth == 8

    def test_port_out_of_range_is_refused(self) -> None:
        assert _parse_deploy_probes(_raw(g={**_GOOD, "ports": [70000]})) == {}

    def test_empty_units_is_refused(self) -> None:
        assert _parse_deploy_probes(_raw(g={**_GOOD, "units": []})) == {}

    def test_one_bad_subject_does_not_take_its_siblings_down(self) -> None:
        probes = _parse_deploy_probes(
            _raw(good=_GOOD, bad={**_GOOD, "max_depth": 99}),
        )
        assert set(probes) == {"good"}

    def test_structurally_wrong_container_is_fatal(self) -> None:
        with pytest.raises(ConfigError):
            _parse_deploy_probes({"investigate": {"deploy": "nope"}})


class TestUnitJudge:
    def test_absent_unit_reads_not_found(self) -> None:
        state = judge_unit_show(
            "system",
            "LoadState=not-found\nActiveState=inactive\nFragmentPath=\n",
        )
        assert not state.installed
        assert not state.running

    def test_running_user_unit(self) -> None:
        state = judge_unit_show(
            "user",
            "LoadState=loaded\nActiveState=active\n"
            "FragmentPath=/home/storm/.config/systemd/user/g.service\n",
        )
        assert state.installed
        assert state.running
        assert state.fragment_path.endswith("g.service")

    def test_loaded_but_dead_is_installed_and_not_running(self) -> None:
        state = judge_unit_show(
            "system", "LoadState=loaded\nActiveState=failed\nFragmentPath=/x\n",
        )
        assert state.installed
        assert not state.running


class TestProcessJudge:
    _PS = (
        "  101   101 /usr/bin/storm-buckets-guard --config /etc/guard.toml\n"
        "  202   202 /usr/bin/postgres\n"
        "  303   303 /usr/bin/less /var/log/storm-buckets-guard.log\n"
    )

    def test_real_match_is_reported(self) -> None:
        found = judge_processes(self._PS, "storm-buckets-guard", frozenset(), 999)
        assert [p.pid for p in found] == [101]

    def test_a_command_line_that_merely_mentions_the_subject_is_not_a_sighting(
        self,
    ) -> None:
        # Earned by the 2026-09-07 smoke run, which reported CLEARED (a guard
        # process is on the box) on a box where the only matches were a log
        # path in an unrelated program's arguments. The question is what is
        # RUNNING, so the test is the program, not the command line.
        found = judge_processes(self._PS, "storm-buckets-guard", frozenset(), 999)
        assert all(p.pid != 303 for p in found)

    def test_own_pid_is_suppressed_even_in_another_process_group(self) -> None:
        found = judge_processes(self._PS, "storm-buckets-guard", frozenset({101}), 777)
        assert found == ()

    def test_own_process_group_is_suppressed_even_at_a_different_pid(self) -> None:
        ps = "  404   999 /usr/bin/storm-buckets-guard\n" + self._PS
        found = judge_processes(ps, "storm-buckets-guard", frozenset(), 999)
        assert [p.pid for p in found] == [101]

    def test_an_ancestor_is_suppressed(self) -> None:
        # The harness that launched the probe carries the subject in its own
        # program name and sits in a different process group; pid and group
        # filters both miss it.
        ps = "  700   700 /usr/bin/storm-buckets-guard-wrapper\n" + self._PS
        found = judge_processes(
            self._PS + ps, "storm-buckets-guard", frozenset({700}), 999,
        )
        assert all(p.pid != 700 for p in found)

    def test_no_match_is_empty(self) -> None:
        assert judge_processes(self._PS, "nothing-like-this", frozenset(), 1) == ()


class TestSelfLineage:
    def test_walks_to_the_root(self) -> None:
        parents = {50: 40, 40: 30, 30: 1, 1: None}
        assert self_lineage(50, parents.get) == frozenset({50, 40, 30, 1})

    def test_unreadable_parent_stops_the_walk(self) -> None:
        assert self_lineage(50, lambda pid: None) == frozenset({50})

    def test_a_cyclic_parent_map_terminates(self) -> None:
        cycle = {50: 40, 40: 50}
        assert self_lineage(50, cycle.get) == frozenset({50, 40})


class TestListenerJudge:
    _SS = (
        "Netid State  Recv-Q Send-Q Local:Port Peer:Port Process\n"
        "tcp   LISTEN 0      4096   0.0.0.0:6188 0.0.0.0:* users:((\"guard\",pid=101))\n"
        "tcp   LISTEN 0      4096   127.0.0.1:5432 0.0.0.0:* users:((\"postgres\",pid=202))\n"
    )

    def test_configured_port_is_found(self) -> None:
        found = judge_listeners(self._SS, (6188,))
        assert "6188" in found[6188]

    def test_unlistened_port_reads_empty(self) -> None:
        assert judge_listeners(self._SS, (6199,)) == {6199: ""}

    def test_other_ports_are_not_reported(self) -> None:
        assert set(judge_listeners(self._SS, (6188,))) == {6188}


class TestArtifactJudge:
    def test_splits_on_the_expected_root(self) -> None:
        inside = Artifact(Path("/home/storm/guard/g"), 1, datetime(2026, 1, 1), True)
        outside = Artifact(
            Path("/home/storm/buckets-guard/g"), 2, datetime(2026, 1, 1), False,
        )
        assert judge_artifacts((inside, outside)) == ((inside,), (outside,))


class TestBoundedWalk:
    def test_finds_a_match_inside_a_root(self, tmp_path: Path) -> None:
        (tmp_path / "guard").mkdir()
        (tmp_path / "guard" / "storm-guard").write_bytes(b"x" * 10)
        found = walk_artifacts(
            (tmp_path,), "guard", tmp_path / "guard", max_depth=3,
        )
        assert [a.path.name for a in found.artifacts] == ["storm-guard"]
        assert found.artifacts[0].size == 10
        assert found.artifacts[0].inside_expected_root
        assert not found.capped

    def test_residue_outside_the_expected_root_is_reported_as_outside(
        self, tmp_path: Path,
    ) -> None:
        # The 2026-09-07 alpha shape: the unit installs to guard/, the disk
        # holds buckets-guard/.
        (tmp_path / "buckets-guard").mkdir()
        (tmp_path / "buckets-guard" / "storm-buckets-guard").write_bytes(b"x")
        found = walk_artifacts(
            (tmp_path,), "guard", tmp_path / "guard", max_depth=3,
        )
        assert len(found.artifacts) == 1
        assert not found.artifacts[0].inside_expected_root
        assert not found.capped

    def test_does_not_descend_past_max_depth(self, tmp_path: Path) -> None:
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "guard-binary").write_bytes(b"x")
        assert walk_artifacts((tmp_path,), "guard", tmp_path, max_depth=2).artifacts == ()
        assert len(walk_artifacts((tmp_path,), "guard", tmp_path, max_depth=3).artifacts) == 1

    def test_does_not_follow_a_directory_symlink_out_of_the_root(
        self, tmp_path: Path,
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "guard-secret").write_bytes(b"x")
        root = tmp_path / "root"
        root.mkdir()
        os.symlink(outside, root / "escape")
        assert walk_artifacts((root,), "guard", root, max_depth=5).artifacts == ()

    def test_does_not_report_a_symlinked_file(self, tmp_path: Path) -> None:
        target = tmp_path / "elsewhere-guard"
        target.write_bytes(b"x")
        root = tmp_path / "root"
        root.mkdir()
        os.symlink(target, root / "guard-link")
        assert walk_artifacts((root,), "guard", root, max_depth=5).artifacts == ()

    def test_missing_root_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        assert walk_artifacts(
            (tmp_path / "nope",), "guard", tmp_path, max_depth=3,
        ).artifacts == ()

    def test_result_cap_holds(self, tmp_path: Path) -> None:
        for i in range(10):
            (tmp_path / f"guard-{i}").write_bytes(b"x")
        found = walk_artifacts(
            (tmp_path,), "guard", tmp_path, max_depth=1, max_results=4,
        )
        assert len(found.artifacts) == 4
        assert found.capped, "a capped walk must say so, or an absence it did not observe reads as CLEARED"


class TestTruncationIsNeverSilent:
    """A cut buffer may not become an absence (CORE-009 decision 4, and
    ``stormpulse/events.py``'s standing rule).

    The asymmetry each of these pins: a positive sighting survives truncation,
    because a short buffer cannot invent a process or a socket. An absence does
    not, because a short buffer is exactly how one is manufactured.
    """

    def test_bounded_flags_the_cut(self) -> None:
        fetched = _bounded("x" * 100, max_bytes=10)
        assert fetched is not None
        assert fetched.truncated
        assert len(fetched.text) == 10

    def test_bounded_does_not_flag_output_under_the_cap(self) -> None:
        fetched = _bounded("short", max_bytes=1024)
        assert fetched is not None
        assert not fetched.truncated
        assert fetched.text == "short"

    def test_a_cut_process_list_with_no_match_is_inconclusive(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "stormpulse.cli.investigate.deploy.run_evidence",
            lambda argv: "1 1 /usr/bin/something-else\n" * 500,
        )
        reports: list[Any] = []
        _report_processes("guard", _probe(max_bytes=32), reports,
                          frozenset(), 0)
        assert [r.verdict.name for r in reports] == ["INCONCLUSIVE"]
        assert reports[0].remedy

    def test_an_uncut_process_list_with_no_match_still_implicates(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "stormpulse.cli.investigate.deploy.run_evidence",
            lambda argv: "1 1 /usr/bin/something-else",
        )
        reports: list[Any] = []
        _report_processes("guard", _probe(), reports, frozenset(), 0)
        assert [r.verdict.name for r in reports] == ["IMPLICATED"]

    def test_a_sighting_survives_truncation(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "stormpulse.cli.investigate.deploy.run_evidence",
            lambda argv: "7 7 /home/storm/guard/guard --serve\n" + "x" * 4000,
        )
        reports: list[Any] = []
        _report_processes("guard", _probe(max_bytes=64), reports,
                          frozenset(), 0)
        assert [r.verdict.name for r in reports] == ["CLEARED"]

    def test_a_cut_listener_table_does_not_implicate_a_port(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "stormpulse.cli.investigate.deploy.run_evidence",
            lambda argv: "tcp LISTEN 0 4096 127.0.0.1:9999 0.0.0.0:*\n" * 200,
        )
        reports: list[Any] = []
        _report_listeners("guard", _probe(ports=(6188,), max_bytes=48), reports)
        assert [r.verdict.name for r in reports] == ["INCONCLUSIVE"]

    def test_a_capped_walk_does_not_clear_the_outside_root(
        self, tmp_path: Path,
    ) -> None:
        for i in range(10):
            (tmp_path / f"guard-{i}").write_bytes(b"x")
        walk = walk_artifacts(
            (tmp_path,), "guard", tmp_path, max_depth=1, max_results=4,
        )
        assert walk.capped
        # Everything found sits inside the expected root, so the naive read is
        # "nothing outside" - which is precisely the CLEARED this must refuse.
        _, outside = judge_artifacts(walk.artifacts)
        assert outside == ()


def _probe(
    *,
    ports: tuple[int, ...] = (),
    max_bytes: int = 65_536,
):
    from stormpulse.config import DeployProbeConfig
    return DeployProbeConfig(
        subject="guard",
        units=("guard.service",),
        expected_root=Path("/home/storm/guard"),
        search_roots=(Path("/home/storm"),),
        ports=ports,
        max_depth=3,
        max_bytes=max_bytes,
    )


class TestContributedSubjects:
    """CORE-009 decision 10: an Integration contributes a default, the node
    overrides it, and precedence only ever points one way.

    The property under test is not "merging works". It is that a package can
    widen nothing the operator has narrowed, which is the only reason a
    contributed subject is allowed to exist.
    """

    def _sdk(self) -> SdkDeploySubject:
        return SdkDeploySubject(
            subject="storm-buckets-guard",
            units=("storm-buckets-guard.service",),
            expected_root="/home/storm/guard",
            search_roots=("/home/storm", "/opt/storm"),
            ports=(6188,),
        )

    def test_a_contributed_subject_needs_no_node_table(self) -> None:
        from stormpulse.config import merge_deploy_probes
        probes = merge_deploy_probes({}, (self._sdk(),))
        assert set(probes) == {"storm-buckets-guard"}
        assert probes["storm-buckets-guard"].ports == (6188,)

    def test_the_node_narrows_one_field_and_keeps_the_rest(self) -> None:
        from stormpulse.config import merge_deploy_probes
        probes = merge_deploy_probes(
            {"storm-buckets-guard": {"search_roots": ["/home/storm"]}},
            (self._sdk(),),
        )
        probe = probes["storm-buckets-guard"]
        assert probe.search_roots == (Path("/home/storm"),), (
            "the operator's narrowing must survive the merge"
        )
        assert probe.units == ("storm-buckets-guard.service",), (
            "unnamed fields must still come from the contributed default"
        )

    def test_a_partial_node_table_is_legal_as_an_override(self) -> None:
        """Alone it is not a valid subject; over a default it is a narrowing.

        This is why the merge happens before validation, and it fails loudly if
        anyone moves validation earlier.
        """
        from stormpulse.config import merge_deploy_probes
        probes = merge_deploy_probes(
            {"storm-buckets-guard": {"max_depth": 1}}, (self._sdk(),),
        )
        assert probes["storm-buckets-guard"].max_depth == 1

    def test_enabled_false_switches_a_contributed_subject_off(self) -> None:
        from stormpulse.config import merge_deploy_probes
        probes = merge_deploy_probes(
            {"storm-buckets-guard": {"enabled": False}}, (self._sdk(),),
        )
        assert probes == {}, "the operator's brake must remove the subject"

    def test_a_node_only_subject_still_works_with_no_contribution(self) -> None:
        from stormpulse.config import merge_deploy_probes
        probes = merge_deploy_probes({"solo": dict(_GOOD)}, ())
        assert set(probes) == {"solo"}

    def test_an_invalid_merge_is_skipped_not_fatal(self) -> None:
        """One broken subject may not take the others down, matching the
        [[log_groups]] precedent the loader already follows."""
        from stormpulse.config import merge_deploy_probes
        probes = merge_deploy_probes(
            {"storm-buckets-guard": {"expected_root": "/somewhere/else"}},
            (self._sdk(),),
        )
        assert probes == {}


class TestContributionGate:
    """`contributed_subjects` asks only ENABLED integrations, and never dies.

    Probing for a thing whose integration is switched off manufactures
    IMPLICATED rows about something the operator deliberately does not run, and
    a board that cries about a subject nobody asked for teaches him to stop
    reading it. That is the failure ADR BUCKETS-043 decision 4 already names.
    """

    def _integ(self, *, enabled: bool, raises: bool = False):
        from stormpulse.sdk import SdkDeploySubject
        subject = SdkDeploySubject(
            subject="guard",
            units=("guard.service",),
            expected_root="/home/storm/guard",
            search_roots=("/home/storm",),
        )

        class FakeIntegration:
            id = "fake"

            @staticmethod
            def parse_config(raw):
                return raw

            @staticmethod
            def enabled(_own):
                return enabled

            @staticmethod
            def deploy_subjects(_own):
                if raises:
                    raise RuntimeError("descriptor exploded")
                return (subject,)

        return FakeIntegration()

    def _cfg(self):
        class FakeConfig:
            integrations = {"fake": {"some": "value"}}
        return FakeConfig()

    def _patch(self, monkeypatch, integ):
        import stormpulse.integrations
        monkeypatch.setattr(
            stormpulse.integrations, "registered_integrations", lambda: [integ],
        )

    def test_an_enabled_integration_contributes(self, monkeypatch) -> None:
        from stormpulse.cli.investigate.deploy import contributed_subjects
        self._patch(monkeypatch, self._integ(enabled=True))
        assert len(contributed_subjects(self._cfg())) == 1

    def test_a_disabled_integration_contributes_nothing(self, monkeypatch) -> None:
        from stormpulse.cli.investigate.deploy import contributed_subjects
        self._patch(monkeypatch, self._integ(enabled=False))
        assert contributed_subjects(self._cfg()) == ()

    def test_a_raising_descriptor_is_skipped_not_fatal(self, monkeypatch) -> None:
        from stormpulse.cli.investigate.deploy import contributed_subjects
        self._patch(monkeypatch, self._integ(enabled=True, raises=True))
        assert contributed_subjects(self._cfg()) == ()
