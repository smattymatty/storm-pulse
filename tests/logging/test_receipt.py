"""Tests for ``holds_back``: which in-flight receipts gate a filtered checkpoint."""

from __future__ import annotations

import pytest

from stormpulse.logging.receipt import FileGeneration, Receipt, holds_back


def _file(
    start: int, end: int, *, inode: int = 7, group: str = "g", src: str = "/a"
) -> Receipt:
    return Receipt(group, src, FileGeneration(inode, 4, b"head"), start, end)


@pytest.mark.parametrize(
    ("pending", "held"),
    [
        pytest.param(_file(0, 10), True, id="same-generation-below"),
        pytest.param(_file(20, 30), False, id="same-generation-at-or-past-end"),
        pytest.param(_file(10, 40), True, id="same-generation-straddling-end"),
        pytest.param(_file(0, 10, inode=8), False, id="another-inode"),
        pytest.param(_file(0, 10, group="h"), False, id="another-group"),
        pytest.param(_file(0, 10, src="/b"), False, id="another-source"),
    ],
)
def test_file_receipts_hold_back_only_their_own_generation_below_the_end(
    pending: Receipt, *, held: bool
) -> None:
    assert holds_back(pending, _file(0, 20)) is held


def test_unordered_cursors_always_hold_back_their_own_source() -> None:
    read = Receipt("g", "unit", None, "c1", "c9")
    assert holds_back(Receipt("g", "unit", None, "c9", "c10"), read)
    assert not holds_back(Receipt("g", "other", None, "c1", "c2"), read)
