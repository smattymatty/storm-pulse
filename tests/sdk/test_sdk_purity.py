"""C5 / I1: ``stormpulse.sdk`` is pure Foundation.

Importing the SDK in a fresh interpreter must pull in NO other ``stormpulse.*``
module (no Framework, Feature, or Entry code). This is the fence that lets P3's
dynamic loader trust the layer, and it is what makes the SDK safe for external
plugin code to import.
"""

from __future__ import annotations

import subprocess
import sys


def test_importing_sdk_pulls_in_no_other_stormpulse_module() -> None:
    # A fresh interpreter: import the SDK, then list every stormpulse module that
    # ended up loaded. Only ``stormpulse`` itself and ``stormpulse.sdk[.*]`` are
    # allowed.
    code = (
        "import stormpulse.sdk, sys;"
        "mods=sorted(m for m in sys.modules"
        " if m=='stormpulse' or m.startswith('stormpulse.'));"
        "print('\\n'.join(mods))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = [m for m in out.stdout.splitlines() if m]
    offenders = [
        m
        for m in loaded
        if m != "stormpulse" and not m.startswith("stormpulse.sdk")
    ]
    assert offenders == [], f"stormpulse.sdk pulled in non-SDK modules: {offenders}"


def test_sdk_source_has_no_host_mutation_primitive() -> None:
    # A source-level companion to the fitness check: the SDK carries no import of
    # a host-mutating primitive. Kept here too so it fails fast under pytest.
    import pathlib

    import stormpulse.sdk as sdk_pkg

    root = pathlib.Path(sdk_pkg.__file__).resolve().parent
    forbidden = ("import subprocess", "import importlib", "importlib.import_module")
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in text, f"{path.name} contains forbidden {needle!r}"
