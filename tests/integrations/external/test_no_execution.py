"""The no-execution fence catches reintroduced primitives; the loader never runs
package code."""

from __future__ import annotations

from pathlib import Path

import pytest

from fitness.external_loader_p1 import check_external_loader_no_execution, scan_source
from stormpulse.integrations.external import inspection, install
from tests.integrations.external._helpers import approve, keypair, make_package, state_dir


def test_fence_passes_on_current_code() -> None:
    assert check_external_loader_no_execution() == []


@pytest.mark.parametrize(
    "snippet",
    [
        "import importlib\n",
        "import runpy\n",
        "from importlib import import_module\n",
        "x = eval('1')\n",
        "exec('x = 1')\n",
        "import sys\nsys.path.append('/x')\n",
        "import sys\nsys.path.insert(0, '/x')\n",
        "import sys\nsys.path.extend(['/x'])\n",
        "import sys\nsys.path = []\n",
        "import sys\nsys.path += ['/x']\n",
    ],
)
def test_fence_catches_each_forbidden_primitive(snippet: str) -> None:
    assert scan_source("fixture.py", snippet) != []


def test_fence_ignores_lookalikes() -> None:
    source = "from ast import literal_eval\ny = literal_eval('1')\nexec_count = 1\n"
    assert scan_source("fixture.py", source) == []


def test_inspect_never_executes_package_code(tmp_path: Path) -> None:
    private, fingerprint = keypair()
    state = state_dir(tmp_path)
    approve(state, tmp_path, private)
    src = tmp_path / "src"
    make_package(src, private, fingerprint, body=b"raise RuntimeError('must never run')\n")
    inspection.inspect_package(src, state)  # must not raise from executing the body


def test_install_never_executes_package_code(tmp_path: Path) -> None:
    private, fingerprint = keypair()
    state = state_dir(tmp_path)
    approve(state, tmp_path, private)
    src = tmp_path / "src"
    make_package(src, private, fingerprint, body=b"raise RuntimeError('must never run')\n")
    install.commit_install(src, state_dir=state, agent_id="a")  # must not raise
