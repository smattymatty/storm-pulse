"""Deriving a deploy subject from a unit file (CORE-009 decision 11).

Framework layer (CORE-000): imports Foundation (``sdk``) only. Lives here and
not beside the investigation because authoring config is the wizard's job and
running the probe is the CLI's, and Framework may not import a Feature or the
CLI above it.

**Why the unit file is the right source and not merely the convenient one.** A
systemd unit *is* the declaration of where its thing lives: ``WorkingDirectory``
and ``ExecStart`` name the install root in the operator's own words, already
reviewed, already deployed. Deriving ``expected_root`` from it is what makes
CORE-009 decision 5's finding possible, because anything found outside it is
then outside by the node's own account rather than by a path someone typed from
memory. On 2026-09-07 three separate hand measurements of one box disagreed
about that path; none of them asked the unit.

Pure by construction: every function here takes ``systemctl show`` text and
returns data. The host read belongs to the caller, so the derivation is
mutation-testable with string fixtures and no box, the same seam the
investigation's judges use.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from stormpulse.sdk import SdkDeploySubject

# Properties the derivation reads. Asked for by name so the parse is a fixed
# shape rather than whatever systemd's default output happens to include.
UNIT_PROPERTIES = ("FragmentPath", "WorkingDirectory", "ExecStart")


def parse_unit_properties(text: str) -> dict[str, str]:
    """``systemctl show`` key=value output into a plain mapping.

    Values may contain '=' (``ExecStart`` routinely does), so only the first
    separator splits. An absent property yields no key rather than an empty
    one, so a caller can tell "systemd said nothing" from "systemd said empty".
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip():
            out[key.strip()] = value.strip()
    return out


def executable_path(exec_start: str) -> Path | None:
    """The absolute binary out of an ``ExecStart`` value, if there is one.

    systemd renders this two ways: the bare command line on a plain unit, and a
    ``{ path=/x ; argv[]=... }`` structure on others. Both are handled, and a
    relative or prefixed path (``-``, ``@``, ``!``) yields None rather than a
    guess: a wrong root would send the probe looking somewhere else entirely
    and report a confident absence about the wrong directory.
    """
    text = exec_start.strip()
    if not text:
        return None
    if "path=" in text:
        text = text.split("path=", 1)[1].split(";", 1)[0].strip()
    else:
        try:
            parts = shlex.split(text)
        except ValueError:
            return None
        if not parts:
            return None
        text = parts[0]
    text = text.lstrip("-@!+")
    if not text.startswith("/"):
        return None
    return Path(text)


def derive_subject(
    unit: str,
    properties: dict[str, str],
    search_roots: tuple[str, ...],
) -> SdkDeploySubject | None:
    """Propose a subject for ``unit``, or None when the unit does not say.

    ``expected_root`` prefers ``WorkingDirectory`` and falls back to the
    directory holding ``ExecStart``'s binary. Returning None on silence is
    deliberate: an invented root produces a probe that reports cleanly about a
    place nothing was ever installed, which is indistinguishable from working
    and is the exact failure this whole investigation exists to end.

    ``search_roots`` stays the caller's, never derived. Widening what a node may
    look at is an operator decision, and a unit file has no opinion about it.
    """
    subject = unit.rsplit(".", 1)[0] if "." in unit else unit
    if not subject:
        return None

    root: Path | None = None
    working = properties.get("WorkingDirectory", "").strip()
    if working.startswith("/"):
        root = Path(working)
    else:
        binary = executable_path(properties.get("ExecStart", ""))
        if binary is not None:
            root = binary.parent

    # `/` is never an expected_root, whichever property produced it. A unit that
    # merely lives at the filesystem root would otherwise point the bounded walk
    # at everything, turning a scoped probe into the home-wide search decision 4
    # exists to refuse. One rule for both branches: the ExecStart path carried
    # this guard alone until an isolated test showed WorkingDirectory bypassed
    # it (2026-09-07).
    if root is None or root == Path("/"):
        return None

    # A proposal the probe would refuse at load is not a proposal. expected_root
    # must sit inside a search root (config.py enforces it), so a derived root
    # outside every one of them is dropped here rather than written into a file
    # that fails to parse on the next run.
    if not any(root == Path(r) or Path(r) in root.parents for r in search_roots):
        return None

    return SdkDeploySubject(
        subject=subject,
        units=(unit,),
        expected_root=str(root),
        search_roots=tuple(search_roots),
    )
