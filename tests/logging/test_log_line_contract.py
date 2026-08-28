"""The declared log-line contract agrees with what the parsers actually emit.

This is the fitness function for the cross-repo log-line contract. It exists
because a consumer once began reading a key from a `caddy_json` line that no
parser here emits, and the resulting silence lasted until somebody happened to
look, with both repositories' suites green throughout.

Every test here compares a DECLARATION against a real parser run. If one fails,
the declaration and the parser disagree. Fix whichever is wrong; never edit the
assertion to match. The declaration is a published contract and a consumer in
another repository asserts against it.
"""

from __future__ import annotations

import pytest

from stormpulse.logging.parsers import PARSERS
from stormpulse.logging.wire_shape import (
    DECLARED,
    GOLDEN_LINES,
    SHIPPER_ADDED,
    build_log_line_contract,
    emitted_fields,
)

_VARIANTS = [
    (parser, variant)
    for parser, variants in sorted(DECLARED.items())
    for variant in sorted(variants)
]


@pytest.mark.parametrize(("parser", "variant"), _VARIANTS)
def test_declared_fields_match_the_parser_output(parser: str, variant: str) -> None:
    """The contract says what the code does, proven by running the code."""
    line = GOLDEN_LINES[parser][variant]
    result = PARSERS[parser](line)
    assert result is not None, (
        f"the golden line for {parser}/{variant} no longer parses. The sample "
        f"is a captured real shape; if the parser legitimately stopped "
        f"accepting it, replace the sample in the same commit."
    )
    assert set(result) == set(DECLARED[parser][variant]), (
        f"{parser}/{variant} emits {sorted(set(result))} but declares "
        f"{sorted(DECLARED[parser][variant])}. A consumer in another repo "
        f"asserts against the declaration, so this drift is invisible to it "
        f"until something silently stops working. Regenerate with "
        f"`make log-line-contract` after fixing."
    )


def test_every_parser_is_declared() -> None:
    """A new parser joins the contract in the commit that adds it."""
    assert set(PARSERS) == set(DECLARED), (
        f"parsers without a declaration: {sorted(set(PARSERS) - set(DECLARED))}; "
        f"declarations without a parser: {sorted(set(DECLARED) - set(PARSERS))}"
    )


def test_golden_lines_cover_every_declared_variant() -> None:
    """A declaration with no golden line is unproven, which is the state this
    whole mechanism exists to make impossible."""
    for parser, variants in DECLARED.items():
        assert set(variants) == set(GOLDEN_LINES.get(parser, {})), (
            f"{parser}: declared variants {sorted(variants)} vs golden lines "
            f"{sorted(GOLDEN_LINES.get(parser, {}))}"
        )


def test_shipper_added_fields_name_a_real_parser() -> None:
    assert set(SHIPPER_ADDED) <= set(PARSERS)


def test_caddy_json_emits_no_bucket() -> None:
    """The known gap, pinned as a fact rather than a memory.

    This is not a wish. It records what the agent does today so a consumer
    cannot assume otherwise, and so that the day someone adds bucket extraction
    to the caddy parser, this test fails and forces the artifact, and every
    consumer's expectation, to move in the same commit.
    """
    assert "bucket" not in emitted_fields("caddy_json")
    assert "path" in emitted_fields("caddy_json")


def test_the_artifact_shape_is_stable() -> None:
    """Consumers index `parsers -> <name> -> variants` and `shipper_added`."""
    art = build_log_line_contract()
    assert art["schema"] == 1
    caddy = art["parsers"]["caddy_json"]
    assert set(caddy) == {"variants", "shipper_added", "all_fields"}
    assert "access" in caddy["variants"]
    assert art["parsers"]["garage_s3"]["shipper_added"] == ["bucket_id"]
    # The precomputed union is what a consumer asserts against, so it must
    # actually contain the shipper half rather than only the parser's own keys.
    assert "bucket_id" in art["parsers"]["garage_s3"]["all_fields"]
    assert "bucket" not in art["parsers"]["caddy_json"]["all_fields"]
