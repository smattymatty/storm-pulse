"""Test integrations against real services at fleet versions, excluded by default.

Apply wire and directory-name markers; run make test-wire or test-<name>-wire.
New integrations need tests/wire/<name>/ with __init__.py and a conftest.py
harness, <name>-up/test-<name>-wire Make targets, and a pyproject.toml marker.
Each integration owns its container; missing services must fail with startup
instructions, never silently skip.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_WIRE_ROOT = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Derive wire and integration markers from paths to keep default runs isolated."""
    for item in items:
        path = Path(str(getattr(item, "fspath", "")))
        try:
            relative = path.relative_to(_WIRE_ROOT)
        except ValueError:
            continue  # not a wire test
        item.add_marker(pytest.mark.wire)
        if len(relative.parts) > 1:
            item.add_marker(getattr(pytest.mark, relative.parts[0]))
