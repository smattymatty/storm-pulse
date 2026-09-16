"""CORE-009 decision 11: the wizard derives a subject from the unit file.

Pure functions over `systemctl show` text, so every case here runs with string
fixtures and no box. The property under test throughout: the derivation either
produces a root the node itself declared, or it produces nothing. It never
guesses, because a guessed root yields a probe that reports cleanly about a
place nothing was installed - which looks exactly like health.
"""
from __future__ import annotations

from pathlib import Path

from stormpulse.wizard.deploy_subject import (
    derive_subject,
    executable_path,
    parse_unit_properties,
)

ROOTS = ("/home/storm", "/opt/storm")


class TestParsing:
    def test_a_value_containing_equals_survives(self) -> None:
        props = parse_unit_properties("ExecStart=/bin/x --flag=1\nWorkingDirectory=/home/storm/guard")
        assert props["ExecStart"] == "/bin/x --flag=1"
        assert props["WorkingDirectory"] == "/home/storm/guard"

    def test_an_absent_property_yields_no_key(self) -> None:
        assert "WorkingDirectory" not in parse_unit_properties("ExecStart=/bin/x")


class TestExecStart:
    def test_plain_command_line(self) -> None:
        assert executable_path("/home/storm/guard/bin --serve") == Path("/home/storm/guard/bin")

    def test_structured_systemd_rendering(self) -> None:
        text = "{ path=/home/storm/guard/bin ; argv[]=/home/storm/guard/bin --serve }"
        assert executable_path(text) == Path("/home/storm/guard/bin")

    def test_prefix_characters_are_stripped(self) -> None:
        assert executable_path("-/home/storm/guard/bin") == Path("/home/storm/guard/bin")

    def test_a_relative_path_is_refused_not_guessed(self) -> None:
        assert executable_path("bin/guard --serve") is None

    def test_empty_is_none(self) -> None:
        assert executable_path("   ") is None


class TestDerivation:
    def test_working_directory_is_preferred(self) -> None:
        got = derive_subject(
            "storm-buckets-guard.service",
            {"WorkingDirectory": "/home/storm/guard", "ExecStart": "/opt/storm/other/bin"},
            ROOTS,
        )
        assert got is not None
        assert got.expected_root == "/home/storm/guard"
        assert got.subject == "storm-buckets-guard"
        assert got.units == ("storm-buckets-guard.service",)

    def test_falls_back_to_the_binary_directory(self) -> None:
        got = derive_subject(
            "guard.service", {"ExecStart": "/home/storm/guard/storm-buckets-guard --serve"}, ROOTS,
        )
        assert got is not None
        assert got.expected_root == "/home/storm/guard"

    def test_a_unit_that_says_nothing_derives_nothing(self) -> None:
        assert derive_subject("guard.service", {}, ROOTS) is None

    def test_a_root_outside_every_search_root_is_dropped(self) -> None:
        """config.py refuses this at load, so proposing it would write a file
        that fails to parse on the next run."""
        assert derive_subject(
            "guard.service", {"WorkingDirectory": "/var/lib/guard"}, ROOTS,
        ) is None

    def test_a_binary_directly_in_slash_is_not_a_root(self) -> None:
        """Isolated deliberately: with ROOTS this passes for the wrong reason.

        `/`'s containment check would reject it anyway, so the test would go
        green with the parent-is-root guard deleted. Passing "/" as the search
        root removes that second defence and leaves only the one being named.
        Proposing "/" as expected_root would point the walk at the whole
        filesystem from a unit that merely lives there.
        """
        assert derive_subject("guard.service", {"ExecStart": "/guard"}, ("/",)) is None
        assert derive_subject(
            "guard.service", {"WorkingDirectory": "/"}, ("/",),
        ) is None

    def test_search_roots_are_the_callers_never_derived(self) -> None:
        got = derive_subject(
            "guard.service", {"WorkingDirectory": "/home/storm/guard"}, ("/home/storm",),
        )
        assert got is not None
        assert got.search_roots == ("/home/storm",)
