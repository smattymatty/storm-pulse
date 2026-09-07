"""The deploy investigation: is this thing actually deployed on this node.

ADR CORE-009. The one fact a node structurally cannot volunteer is its own
absence: a box with no guard has no guard to speak, and residue in a directory
nobody expected has nothing that reports it. Every other investigation here
explains a system that is running; this one answers whether one exists.

Observe, do not judge the expectation. The node reports what it sees - this
unit exists in the user manager, this port has no listener, this binary sits at
this path with this mtime. Whether that is what the box was supposed to be is
the control plane's comparison (CORE-009 decision 2). The fetch/judge split is
unchanged and is a different axis: fetches touch the host, judges are pure
functions over fetched text, so every verdict below is testable with string
fixtures and no box.

Receipt, 2026-09-07: the firm hand-assembled this battery over SSH to settle
whether a guard runs on alpha, burned two rounds on pgrep self-match artifacts,
and found a guard binary at /home/storm/buckets-guard/storm-buckets-guard while
every install site in the repo named /home/storm/guard. The 2026-08-26
measurement had checked a path that has never existed on that box.
"""

from __future__ import annotations

import argparse
import os
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from stormpulse.config import DeployProbeConfig
from stormpulse.sdk.investigate import CaseFile, SuspectReport, Verdict, Window

from ._journal import run_evidence

# ---------------------------------------------------------------------------
# Observations: what a fetch produces and a judge consumes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnitState:
    """One systemd unit as one manager sees it."""

    manager: str  # "system" or "user"
    load_state: str
    active_state: str
    fragment_path: str

    @property
    def installed(self) -> bool:
        return self.load_state not in ("not-found", "")

    @property
    def running(self) -> bool:
        return self.active_state == "active"


@dataclass(frozen=True, slots=True)
class ProcessSighting:
    pid: int
    pgid: int
    args: str


@dataclass(frozen=True, slots=True)
class Artifact:
    """A file the bounded walk found, and where it sits relative to expected."""

    path: Path
    size: int
    mtime: datetime
    inside_expected_root: bool


@dataclass(frozen=True, slots=True)
class Fetched:
    """Command output, and whether the byte cap cut it.

    Truncation is never silent (`stormpulse/events.py`). A judge handed a cut
    buffer cannot tell a real absence from a lost tail, and this investigation
    exists to be trusted about absence, so the flag rides with the text
    instead of being discarded at the cap.

    The rule every caller applies, and the reason the flag alone is enough:
    **a positive sighting survives truncation, an absence does not.** A cut
    buffer cannot manufacture a `LoadState=loaded` or a listening socket, so
    finding one is still evidence. Not finding one, in text known to be
    incomplete, is INCONCLUSIVE and never IMPLICATED.
    """

    text: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class Walk:
    """What the bounded walk saw, and whether the result cap stopped it early."""

    artifacts: tuple[Artifact, ...]
    capped: bool


# ---------------------------------------------------------------------------
# Judges: pure functions over fetched text
# ---------------------------------------------------------------------------


def judge_unit_show(manager: str, text: str) -> UnitState:
    """Parse ``systemctl show`` key=value output into a UnitState.

    An absent unit is not an error: systemd answers LoadState=not-found with a
    zero exit, which is exactly the reading this investigation exists to get.
    """
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    return UnitState(
        manager=manager,
        load_state=fields.get("LoadState", ""),
        active_state=fields.get("ActiveState", ""),
        fragment_path=fields.get("FragmentPath", ""),
    )


_PS_LINE_RE = re.compile(r"^\s*(?P<pid>\d+)\s+(?P<pgid>\d+)\s+(?P<args>.+)$")


def judge_processes(
    ps_text: str,
    needle: str,
    exclude_pids: frozenset[int],
    self_pgid: int,
) -> tuple[ProcessSighting, ...]:
    """Matching processes, with this probe's own family suppressed.

    CORE-009 decision 7: self-match is the contract's problem, not the
    caller's. Two rules, and the smoke run on 2026-09-07 earned both by
    reporting CLEARED on a box carrying no such process at all.

    - **Suppress the probe's own pid, its process group, AND its ancestry.**
      Group alone is not enough: the harness or shell that launched the probe
      often sits in a different group while carrying the subject's name in its
      own command line, so it matched its own search string one level up.
    - **Match the PROGRAM, not the command line.** ``needle in args`` makes
      every process that merely mentions a path a sighting - a log path, a
      working directory, an editor with the file open. The question is whether
      the subject is RUNNING here, so the test is argv[0]'s basename.
    """
    sightings: list[ProcessSighting] = []
    for line in ps_text.splitlines():
        m = _PS_LINE_RE.match(line)
        if m is None:
            continue
        pid = int(m.group("pid"))
        pgid = int(m.group("pgid"))
        args = m.group("args").strip()
        if pid in exclude_pids or pgid == self_pgid:
            continue
        program = args.split()[0] if args.split() else ""
        if needle in Path(program).name:
            sightings.append(ProcessSighting(pid=pid, pgid=pgid, args=args))
    return tuple(sightings)


def self_lineage(start: int, read_ppid: Callable[[int], int | None]) -> frozenset[int]:
    """This process and every ancestor of it, as pids.

    ``read_ppid`` is injected so the walk is a pure function over a parent map
    in tests and reads /proc in production. Bounded by a visited set, so a
    lying or cyclic parent map cannot spin.
    """
    seen: set[int] = set()
    pid: int | None = start
    while pid is not None and pid > 0 and pid not in seen:
        seen.add(pid)
        pid = read_ppid(pid)
    return frozenset(seen)


def read_ppid_from_proc(pid: int) -> int | None:
    """Parent pid from /proc/<pid>/stat, or None if unreadable.

    The comm field is parenthesised and may contain spaces and ')', so the
    fields are taken after the LAST ')' - the standard way to parse this file
    without being fooled by a process named ") 1 2 3".
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    _, sep, rest = raw.rpartition(")")
    if not sep:
        return None
    fields = rest.split()
    if len(fields) < 2 or not fields[1].isdigit():
        return None
    return int(fields[1])


_SS_LINE_RE = re.compile(
    r"^(?P<proto>tcp|udp)\s+\S+\s+\S+\s+\S+\s+(?P<local>\S+)\s+\S+(?P<rest>.*)$"
)


def judge_listeners(ss_text: str, ports: tuple[int, ...]) -> dict[int, str]:
    """Map each configured port to the listener description, or "" if none.

    Reads ``ss -lntup``. A port with no row is not evidence of health either
    way; it is the absence the caller turns into its own verdict.
    """
    found: dict[int, str] = {port: "" for port in ports}
    for line in ss_text.splitlines():
        m = _SS_LINE_RE.match(line.strip())
        if m is None:
            continue
        local = m.group("local")
        _, sep, port_text = local.rpartition(":")
        if not sep or not port_text.isdigit():
            continue
        port = int(port_text)
        if port in found and not found[port]:
            detail = m.group("rest").strip() or m.group("proto")
            found[port] = f"{local} {detail}".strip()
    return found


def judge_artifacts(
    artifacts: tuple[Artifact, ...],
) -> tuple[tuple[Artifact, ...], tuple[Artifact, ...]]:
    """Split found artifacts into (inside expected root, outside it).

    Outside is not an error and not a spec problem: it is the finding
    (CORE-009 decision 5). The box disagreeing with itself is the whole
    signal, and a probe that only checked the expected path would report a
    clean absence and be exactly as wrong as the battery that preceded it.
    """
    inside = tuple(a for a in artifacts if a.inside_expected_root)
    outside = tuple(a for a in artifacts if not a.inside_expected_root)
    return inside, outside


def is_within(candidate: Path, root: Path) -> bool:
    """True when ``candidate`` is ``root`` or sits underneath it."""
    return candidate == root or root in candidate.parents


# ---------------------------------------------------------------------------
# The bounded walk: a fetch, but its bounds are the refusal, so they are
# written to be exercised against a fixture tree with no host involved.
# ---------------------------------------------------------------------------


def walk_artifacts(
    roots: tuple[Path, ...],
    needle: str,
    expected_root: Path,
    max_depth: int,
    max_results: int = 200,
) -> Walk:
    """Find files whose name contains ``needle``, inside the roots, bounded.

    Three bounds, all of them the point (CORE-009 decision 4):

    - **Root allowlist.** Nothing outside ``roots`` is ever opened. Directory
      symlinks are not followed, and a resolved path that escapes its root is
      dropped rather than reported: a symlink is the cheapest way to turn a
      declared bound into no bound at all.
    - **Depth cap.** Relative to each root, so a shallow root cannot be
      widened by a deep one.
    - **Result cap.** A pathological tree cannot turn a case file into a
      directory listing. When it fires the walk says so: `Walk.capped` is the
      difference between "nothing else is there" and "I stopped looking", and
      only the first of those may become a CLEARED.

    The cost of the bound is real and named in the ADR: residue in a root
    nobody thought to name is not found here, and the caller reports
    INCONCLUSIVE with the ``find`` an operator would run by hand.
    """
    out: list[Artifact] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path, depth in _walk_bounded(root, max_depth):
            if len(out) >= max_results:
                return Walk(artifacts=tuple(out), capped=True)
            if needle not in path.name:
                continue
            try:
                stat = path.lstat()
            except OSError:
                continue
            out.append(Artifact(
                path=path,
                size=stat.st_size,
                mtime=datetime.fromtimestamp(stat.st_mtime),
                inside_expected_root=is_within(path, expected_root),
            ))
    return Walk(artifacts=tuple(out), capped=False)


def _walk_bounded(root: Path, max_depth: int) -> Iterator[tuple[Path, int]]:
    """Yield (path, depth) for entries at most ``max_depth`` below ``root``.

    Iterative and symlink-refusing. Refusing every symlink outright, rather
    than resolving each one and re-checking containment, is the cheaper
    correctness argument: there is one rule to verify instead of two, and no
    branch that only a contrived tree could reach. A resolve-and-compare guard
    was written here first and removed because nothing could make it fire.
    """
    frontier: list[tuple[Path, int]] = [(root, 0)]
    while frontier:
        current, depth = frontier.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            path = Path(entry.path)
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                if depth + 1 > max_depth:
                    continue
                frontier.append((path, depth + 1))
                continue
            yield path, depth + 1


# ---------------------------------------------------------------------------
# Fetches: the part that touches the box. Thin, because the thinking above
# already happened.
# ---------------------------------------------------------------------------

_UNIT_PROPERTIES = "LoadState,ActiveState,FragmentPath"


def _bounded(text: str | None, max_bytes: int) -> Fetched | None:
    """Cap a fetch's output, and say so when the cap fired.

    The probe reports a file's existence, size and mtime and never opens it,
    so this cap is a belt on command output, not the mechanism that keeps file
    contents out of a case file. It returns `Fetched` rather than a bare `str`
    because a caller that cannot see the cap fire will state an absence it did
    not observe: `ss` output past the cap on a busy box yields "Nothing
    listening on 6188" as though it were measured.
    """
    if text is None:
        return None
    encoded = text.encode("utf-8", "replace")
    if len(encoded) <= max_bytes:
        return Fetched(text=text, truncated=False)
    return Fetched(
        text=encoded[:max_bytes].decode("utf-8", "ignore"),
        truncated=True,
    )


def _fetch_unit(manager: str, unit: str, max_bytes: int) -> Fetched | None:
    argv = ["systemctl"]
    if manager == "user":
        argv.append("--user")
    argv += ["show", unit, "--no-pager", f"--property={_UNIT_PROPERTIES}"]
    return _bounded(run_evidence(argv), max_bytes)


def _fetch_processes(max_bytes: int) -> Fetched | None:
    return _bounded(run_evidence(["ps", "-eo", "pid,pgid,args", "--no-headers"]), max_bytes)


def _fetch_listeners(max_bytes: int) -> Fetched | None:
    return _bounded(run_evidence(["ss", "-lntup"]), max_bytes)


# ---------------------------------------------------------------------------
# Case file assembly
# ---------------------------------------------------------------------------


def run_deploy(args: argparse.Namespace, window: Window) -> CaseFile:
    """``stormpulse investigate deploy`` - what is actually on this node."""
    from stormpulse.cli.investigate import make_case
    from stormpulse.config import ConfigError, load_config

    reports: list[SuspectReport] = []
    next_moves: list[str] = []
    open_questions: list[str] = []

    try:
        probes = load_config(Path(args.config)).deploy_probes
    except ConfigError as exc:
        return make_case(
            "deploy", window,
            [SuspectReport(
                suspect="node-local probe config",
                verdict=Verdict.INCONCLUSIVE,
                evidence=f"Config unreadable: {exc}",
                detail="Every parameter this investigation uses resolves from "
                       "the node's own config and from nowhere else, so an "
                       "unreadable config means there is nothing to look for.",
                remedy=f"stormpulse config check --config {args.config}",
            )],
            [], [],
        )

    if not probes:
        return make_case(
            "deploy", window,
            [SuspectReport(
                suspect="node-local probe config",
                verdict=Verdict.INCONCLUSIVE,
                evidence=f"No [investigate.deploy.<subject>] section in {args.config}.",
                detail="A node with nothing declared reports INCONCLUSIVE, "
                       "never CLEARED: silence about a subject is not evidence "
                       "that the subject is absent.",
                remedy=f"$EDITOR {args.config}  # add [investigate.deploy.<subject>]",
            )],
            ["Declare what this node is supposed to be carrying, then re-run."],
            [],
        )

    lineage = self_lineage(os.getpid(), read_ppid_from_proc)
    self_pgid = os.getpgid(0)
    for subject, probe in sorted(probes.items()):
        _report_units(subject, probe, reports)
        _report_processes(subject, probe, reports, lineage, self_pgid)
        _report_listeners(subject, probe, reports)
        _report_artifacts(subject, probe, reports, next_moves, open_questions)

    return make_case("deploy", window, reports, next_moves, open_questions)


def _report_units(
    subject: str, probe: DeployProbeConfig, reports: list[SuspectReport],
) -> None:
    """Both managers, every time, never one.

    A system-only check on a rootless box reports "no unit" for a unit that is
    running, which is a false CLEARED - absence of evidence turned into
    permission (CORE-009 decision 6).
    """
    for unit in probe.units:
        states: list[UnitState] = []
        unreadable: list[str] = []
        cut: list[str] = []
        for manager in ("system", "user"):
            fetched = _fetch_unit(manager, unit, probe.max_bytes)
            if fetched is None:
                unreadable.append(manager)
            elif fetched.truncated:
                cut.append(manager)
            else:
                states.append(judge_unit_show(manager, fetched.text))

        installed = [s for s in states if s.installed]
        blind = unreadable + cut
        if not states:
            reports.append(SuspectReport(
                suspect=f"{subject}: unit {unit} not installed",
                verdict=Verdict.INCONCLUSIVE,
                evidence="Neither systemd manager could be read: "
                         f"{', '.join(unreadable) or 'none'} unavailable, "
                         f"{', '.join(cut) or 'none'} cut at the byte cap.",
                remedy=f"systemctl show {unit} --property={_UNIT_PROPERTIES}",
            ))
            continue
        if not installed:
            managers = " and ".join(s.manager for s in states)
            if blind:
                # An absence read from a partial survey is not an absence.
                reports.append(SuspectReport(
                    suspect=f"{subject}: unit {unit} not installed",
                    verdict=Verdict.INCONCLUSIVE,
                    evidence=f"LoadState=not-found in the {managers} manager, "
                             f"but {' and '.join(blind)} could not be read "
                             f"({', '.join(cut)} cut at the byte cap)."
                             if cut else
                             f"LoadState=not-found in the {managers} manager, "
                             f"but {' and '.join(blind)} could not be read.",
                    detail="One manager answering not-found while the other "
                           "went unread is not evidence that no unit exists.",
                    remedy=f"systemctl --user show {unit} "
                           f"--property={_UNIT_PROPERTIES}",
                ))
                continue
            reports.append(SuspectReport(
                suspect=f"{subject}: unit {unit} not installed",
                verdict=Verdict.IMPLICATED,
                evidence=f"LoadState=not-found in the {managers} manager.",
                detail="No unit means nothing supervises this subject on this "
                       "box, whatever else is on disk.",
            ))
            continue

        state = installed[0]
        reports.append(SuspectReport(
            suspect=f"{subject}: unit {unit} not installed",
            verdict=Verdict.CLEARED,
            evidence=f"{state.manager} manager: LoadState={state.load_state}, "
                     f"FragmentPath={state.fragment_path or '(none)'}.",
        ))
        running = [s for s in installed if s.running]
        reports.append(SuspectReport(
            suspect=f"{subject}: unit {unit} installed but not running",
            verdict=Verdict.CLEARED if running else Verdict.IMPLICATED,
            evidence=(
                f"ActiveState={running[0].active_state} in the "
                f"{running[0].manager} manager."
                if running else
                f"ActiveState={state.active_state} in the {state.manager} manager."
            ),
            remedy=(
                "" if running else
                f"systemctl{' --user' if state.manager == 'user' else ''} "
                f"status {unit} --no-pager"
            ),
        ))


def _report_processes(
    subject: str,
    probe: DeployProbeConfig,
    reports: list[SuspectReport],
    exclude_pids: frozenset[int],
    self_pgid: int,
) -> None:
    fetched = _fetch_processes(probe.max_bytes)
    if fetched is None:
        reports.append(SuspectReport(
            suspect=f"{subject}: no process on the box",
            verdict=Verdict.INCONCLUSIVE,
            evidence="`ps` unavailable.",
            remedy=f"ps -eo pid,pgid,args | grep {subject}",
        ))
        return
    sightings = judge_processes(fetched.text, subject, exclude_pids, self_pgid)
    if sightings:
        # A sighting stands whether or not the tail was cut: a truncated
        # buffer cannot invent a process.
        first = sightings[0]
        reports.append(SuspectReport(
            suspect=f"{subject}: no process on the box",
            verdict=Verdict.CLEARED,
            evidence=f"{len(sightings)} process(es); pid {first.pid}: "
                     f"{first.args[:120]}",
        ))
    elif fetched.truncated:
        reports.append(SuspectReport(
            suspect=f"{subject}: no process on the box",
            verdict=Verdict.INCONCLUSIVE,
            evidence=f"`ps` output was cut at {probe.max_bytes} bytes before "
                     "the end, so the process list read here is partial.",
            detail="No match was found in what was read. On a box with enough "
                   "processes to pass the cap, that is a statement about the "
                   "buffer and not about the box.",
            remedy=f"ps -eo pid,pgid,args | grep {subject}",
        ))
    else:
        reports.append(SuspectReport(
            suspect=f"{subject}: no process on the box",
            verdict=Verdict.IMPLICATED,
            evidence="No process whose program name matches, with this "
                     "probe's own pid, process group and ancestry excluded.",
        ))


def _report_listeners(
    subject: str, probe: DeployProbeConfig, reports: list[SuspectReport],
) -> None:
    if not probe.ports:
        return
    fetched = _fetch_listeners(probe.max_bytes)
    if fetched is None:
        reports.append(SuspectReport(
            suspect=f"{subject}: configured ports not listening",
            verdict=Verdict.INCONCLUSIVE,
            evidence="`ss` unavailable.",
            remedy="ss -lntup",
        ))
        return
    listeners = judge_listeners(fetched.text, probe.ports)
    for port, detail in sorted(listeners.items()):
        if detail:
            # Found is found; the cap cannot fabricate a socket.
            reports.append(SuspectReport(
                suspect=f"{subject}: port {port} not listening",
                verdict=Verdict.CLEARED,
                evidence=detail,
            ))
        elif fetched.truncated:
            reports.append(SuspectReport(
                suspect=f"{subject}: port {port} not listening",
                verdict=Verdict.INCONCLUSIVE,
                evidence=f"`ss` output was cut at {probe.max_bytes} bytes and "
                         f"no row for {port} appeared in what was read.",
                detail="The row may sit past the cap. This investigation does "
                       "not turn a short buffer into an absent listener.",
                remedy=f"ss -lntup | grep :{port}",
            ))
        else:
            reports.append(SuspectReport(
                suspect=f"{subject}: port {port} not listening",
                verdict=Verdict.IMPLICATED,
                evidence=f"Nothing listening on {port}.",
            ))


def _report_artifacts(
    subject: str,
    probe: DeployProbeConfig,
    reports: list[SuspectReport],
    next_moves: list[str],
    open_questions: list[str],
) -> None:
    """The bounded walk's verdicts, including the one this probe exists for."""
    walk = walk_artifacts(
        roots=probe.search_roots,
        needle=subject,
        expected_root=probe.expected_root,
        max_depth=probe.max_depth,
    )
    inside, outside = judge_artifacts(walk.artifacts)

    if inside:
        newest = max(inside, key=lambda a: a.mtime)
        reports.append(SuspectReport(
            suspect=f"{subject}: nothing installed at the expected root",
            verdict=Verdict.CLEARED,
            evidence=f"{len(inside)} artifact(s) under {probe.expected_root}; "
                     f"newest {newest.path.name} ({newest.size} bytes, "
                     f"{newest.mtime:%Y-%m-%d %H:%M:%S}).",
        ))
    else:
        roots = ", ".join(str(r) for r in probe.search_roots)
        reports.append(SuspectReport(
            suspect=f"{subject}: nothing installed at the expected root",
            verdict=Verdict.INCONCLUSIVE if walk.capped else Verdict.IMPLICATED,
            evidence=(
                f"The walk stopped at its result cap before finishing "
                f"{roots}, and nothing under {probe.expected_root} had been "
                f"found when it stopped."
                if walk.capped else
                f"No artifact under {probe.expected_root} "
                f"(searched {roots}, depth {probe.max_depth})."
            ),
            detail="The search is bounded to declared roots by design, so this "
                   "is 'not in the places this node names', not 'not on this "
                   "box'. The remedy widens it once, by hand.",
            remedy=f"find $HOME -maxdepth 6 -name '*{subject}*' 2>/dev/null",
        ))
        open_questions.append(
            f"Is {subject} installed somewhere this node does not declare? "
            f"Only the unbounded find above answers that."
        )

    if outside:
        listed = "; ".join(
            f"{a.path} ({a.size} bytes, {a.mtime:%Y-%m-%d %H:%M:%S})"
            for a in outside[:5]
        )
        reports.append(SuspectReport(
            suspect=f"{subject}: artifacts outside the expected root",
            verdict=Verdict.IMPLICATED,
            evidence=f"{len(outside)} artifact(s) outside "
                     f"{probe.expected_root}: {listed}",
            detail="The box disagrees with itself. This is a finding, not a "
                   "misconfiguration of the probe: on 2026-09-07 exactly this "
                   "shape was spike residue that three earlier measurements "
                   "had missed.",
        ))
        next_moves.append(
            f"Decide whether the {subject} artifacts outside "
            f"{probe.expected_root} are residue to remove or an install the "
            f"unit files do not know about."
        )
    elif walk.capped:
        reports.append(SuspectReport(
            suspect=f"{subject}: artifacts outside the expected root",
            verdict=Verdict.INCONCLUSIVE,
            evidence="The walk stopped at its result cap, so the declared "
                     "roots were not searched to the end.",
            detail="Residue outside the expected root is the finding this "
                   "probe exists for. A capped walk that found none has not "
                   "ruled it out, and saying CLEARED here would repeat the "
                   "2026-08-26 mistake with a different bound.",
            remedy=f"find $HOME -maxdepth 6 -name '*{subject}*' 2>/dev/null",
        ))
    else:
        reports.append(SuspectReport(
            suspect=f"{subject}: artifacts outside the expected root",
            verdict=Verdict.CLEARED,
            evidence=f"Nothing matching {subject!r} outside "
                     f"{probe.expected_root} within the declared roots.",
        ))
