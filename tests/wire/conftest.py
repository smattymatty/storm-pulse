"""The wire tier: tests that run against the real system an Integration drives.

One tier, one directory per Integration. ``tests/wire/garage/`` drives a real
Garage; a future ``tests/wire/caddy/`` drives a real Caddy, and so on. Each
brings its own container and its own harness; nothing here knows about any
specific integration.

The tier exists to answer the question no fake can: **does the agent still work
against the real thing, at the version the fleet runs?** A dependency upgrade
that renames a JSON field, changes a status code, or reworks an error string
breaks the agent silently everywhere else in the suite. It breaks here loudly,
before a deploy.

**Markers.** Every test under ``tests/wire/`` gets two, applied automatically
below: ``wire`` (the tier, which ``pyproject.toml`` deselects by default so
``make check`` stays Docker-free) and one named for its integration directory
(``garage``, ``caddy``, ...). That is what makes the Makefile targets compose:

    make test-wire            ->  -m wire                 (every integration)
    make test-garage-wire     ->  -m "wire and garage"    (one)

**Adding an integration.** Create ``tests/wire/<name>/`` with an
``__init__.py``, a ``conftest.py`` holding its harness, and a Makefile pair
(``<name>-up`` / ``test-<name>-wire``). Declare the ``<name>`` marker in
``pyproject.toml``. No change to this file: the marker is derived from the
directory.

**Hard-fail, never skip.** If you asked for this tier and the container is
down, that is an error naming the command that fixes it. The predecessor of
this tier gated itself on unset env vars and skipped, which is why it never
once ran.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_WIRE_ROOT = Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every wire test with ``wire`` plus its integration's name.

    Derived from the directory rather than written by hand, so a new wire test
    cannot leak into the default suite by forgetting the marker, and a new
    integration directory needs no change here.
    """
    for item in items:
        path = Path(str(getattr(item, "fspath", "")))
        try:
            relative = path.relative_to(_WIRE_ROOT)
        except ValueError:
            continue  # not a wire test
        item.add_marker(pytest.mark.wire)
        if len(relative.parts) > 1:
            item.add_marker(getattr(pytest.mark, relative.parts[0]))
