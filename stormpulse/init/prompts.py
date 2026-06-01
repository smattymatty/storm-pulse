"""Interactive prompt helpers for ``stormpulse init``."""

from __future__ import annotations

import grp
import os
import pwd
import re
import sys
import tomllib
from pathlib import Path

from stormpulse.init.checks import InitError
from stormpulse.init.compose import detect_compose_files, parse_service_names


def prompt(message: str, *, default: str | None = None) -> str:
    """Print a prompt to stderr and read from stdin. Raises InitError on EOF."""
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{message}{suffix}: ").strip()
    except EOFError as exc:
        raise InitError("Input ended unexpectedly (EOF)") from exc
    return value if value else (default or "")


def prompt_confirm(message: str, *, default_yes: bool = True) -> bool:
    """Yes/no prompt. Returns True for yes."""
    hint = "Y/n" if default_yes else "y/N"
    response = prompt(f"{message} [{hint}]")
    if not response:
        return default_yes
    return response.lower() in ("y", "yes")


UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _read_existing_pulse_token(config_path: Path | None) -> str | None:
    """Return the previously-configured pulse token, if any.

    On re-runs of ``stormpulse init`` the token rarely changes but the
    operator otherwise has to dig it out of the dashboard and retype it
    every time. We read ``[agent].pulse_token`` from a prior config and
    validate it against ``UUID_RE`` -- a stale or hand-edited token
    that wouldn't pass the prompt's validation either is skipped so
    the operator isn't fooled into pressing Enter on a broken default.
    Returns None on any failure; the caller falls back to no default.
    """
    if config_path is None or not config_path.is_file():
        return None
    try:
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    token = data.get("agent", {}).get("pulse_token")
    if isinstance(token, str) and UUID_RE.match(token):
        return token
    return None


def prompt_pulse_token(remembered_from: Path | None = None) -> str:
    """Prompt for the pulse token (UUID format).

    If a previous config exists at ``remembered_from`` and contains a
    valid pulse token, offer it as the default so a re-run of
    ``stormpulse init`` only needs an Enter press to keep the same
    token.
    """
    default = _read_existing_pulse_token(remembered_from)
    while True:
        value = prompt("Pulse token (from dashboard)", default=default)
        if UUID_RE.match(value):
            return value
        print(
            "  Invalid format - expected a UUID (e.g. a1b2c3d4-5678-...)",
            file=sys.stderr,
        )


def prompt_dashboard_url(default: str | None = None) -> str:
    """Prompt for the dashboard WebSocket URL."""
    while True:
        value = prompt("Dashboard WebSocket URL", default=default)
        if value.startswith("wss://") or value.startswith("ws://"):
            if value.startswith("ws://"):
                print(
                    "  Warning: ws:// is unencrypted. Use wss:// in production.",
                    file=sys.stderr,
                )
            return value
        print("  URL must start with wss:// or ws://", file=sys.stderr)


def prompt_project_dir() -> Path:
    """Prompt for the project directory; resolve missing or unowned paths.

    Fresh-box workflow: the operator is often installing the agent
    before the project itself exists, or just after ``sudo mkdir``'d
    the dir without remembering to chown it. Re-prompting silently
    made them drop the wizard and start over.

    Three branches:

    - Path exists and is writable → return it.
    - Path exists but is *not* writable (e.g. ``sudo mkdir`` without
      chown) → print ``sudo chown -R $USER:$USER <path>`` and a
      Press-Enter-to-retry prompt defaulting to the same path.
    - Path doesn't exist → check the deepest existing ancestor.
      Writable → ``Create it? [Y/n]`` → ``mkdir -p`` + report owner.
      Not writable → print ``sudo mkdir -p ... && sudo chown ...``
      and the same Press-Enter retry.

    Never escalates -- no subprocess sudo, no password capture. The
    agent's privilege surface stays at zero.
    """
    value = prompt("Project directory", default=str(Path.cwd()))
    while True:
        p = Path(value)
        if p.is_dir():
            if os.access(p, os.W_OK):
                return p.resolve()
            # Exists but the operator can't write to it. Downstream
            # steps (scaffold compose, write systemd unit drop-in,
            # etc.) all assume project_dir is theirs. Catch it here
            # with the same chown + Press-Enter pattern used for
            # missing dirs -- consistent UX, no escalation.
            print(
                f"  Directory {value} exists but is not writable by you.",
                file=sys.stderr,
            )
            print("  Run this in another shell:", file=sys.stderr)
            print(f"    sudo chown -R $USER:$USER {value}", file=sys.stderr)
            value = prompt(
                f"  Press Enter once {value} is yours, or type a different path",
                default=value,
            )
            continue
        print(f"  Directory {value} does not exist.", file=sys.stderr)
        ancestor = p
        while not ancestor.exists() and ancestor.parent != ancestor:
            ancestor = ancestor.parent
        if ancestor.exists() and os.access(ancestor, os.W_OK):
            if not prompt_confirm("  Create it?", default_yes=True):
                value = prompt("Project directory", default=str(Path.cwd()))
                continue
            p.mkdir(parents=True, exist_ok=True)
            try:
                st = p.stat()
                owner = pwd.getpwuid(st.st_uid).pw_name
                group = grp.getgrgid(st.st_gid).gr_name
                print(f"  Created {p} (owner: {owner}:{group})", file=sys.stderr)
            except KeyError:
                print(f"  Created {p}", file=sys.stderr)
            return p.resolve()
        print(
            f"  Cannot create {value} -- parent directory not writable.",
            file=sys.stderr,
        )
        print("  Run this in another shell:", file=sys.stderr)
        print(
            f"    sudo mkdir -p {value} && sudo chown $USER:$USER {value}",
            file=sys.stderr,
        )
        value = prompt(
            f"  Press Enter once {value} exists, or type a different path",
            default=value,
        )


def prompt_compose_file(project_dir: Path) -> Path:
    """Auto-detect compose files, let user pick or enter manually.

    On a fresh agent-first install, the compose file often doesn't
    exist yet (the operator is wiring the agent before installing the
    project's docker stack). When ``detect_compose_files`` returns
    nothing and the project dir is writable, we offer to scaffold a
    placeholder with one service block so the wizard can finish.
    The placeholder uses ``image: placeholder:latest`` -- a
    deliberately non-resolvable image so ``docker compose up`` fails
    loudly until the operator replaces it.
    """
    found = detect_compose_files(project_dir)
    if len(found) == 1:
        confirm = prompt(f"Compose file: {found[0]}? (y/n)", default="y")
        if confirm.lower() in ("y", "yes", ""):
            return found[0]
    elif len(found) > 1:
        print("  Found multiple compose files:", file=sys.stderr)
        for i, p in enumerate(found, 1):
            print(f"    {i}. {p}", file=sys.stderr)
        while True:
            choice = prompt(f"Pick 1-{len(found)}, or enter a path")
            if choice.isdigit() and 1 <= int(choice) <= len(found):
                return found[int(choice) - 1]
            p = Path(choice)
            if p.is_file():
                return p.resolve()
            print(f"  Not found: {choice}", file=sys.stderr)
    else:
        scaffolded = _offer_compose_scaffold(project_dir)
        if scaffolded is not None:
            return scaffolded

    # Manual entry: declined single auto-detect, or scaffold declined/unavailable.
    while True:
        value = prompt("Path to docker-compose file")
        p = Path(value)
        if p.is_file():
            return p.resolve()
        print(f"  File not found: {value}", file=sys.stderr)


def _offer_compose_scaffold(project_dir: Path) -> Path | None:
    """Offer to write a placeholder ``docker-compose.yml`` in project_dir.

    Returns the resolved path on success, or ``None`` if the operator
    declined or the project dir isn't writable (in which case the
    caller falls through to manual entry). Never escalates: if the
    dir is not writable we skip the offer rather than try sudo.
    """
    target = project_dir / "docker-compose.yml"
    print(f"  No compose file found in {project_dir}.", file=sys.stderr)
    if not os.access(project_dir, os.W_OK):
        # Can't write here -- don't offer something we can't do.
        # Manual entry remains the escape hatch.
        return None
    if not prompt_confirm(
        f"  Scaffold a placeholder at {target}?",
        default_yes=True,
    ):
        return None
    default_service = project_dir.name or "app"
    service_name = prompt("  Service name", default=default_service)
    if not service_name:
        service_name = default_service
    target.write_text(
        "# stormpulse init placeholder -- replace before starting the service.\n"
        "services:\n"
        f"  {service_name}:\n"
        "    image: placeholder:latest\n"
    )
    try:
        st = target.stat()
        owner = pwd.getpwuid(st.st_uid).pw_name
        group = grp.getgrgid(st.st_gid).gr_name
        print(
            f"  Wrote placeholder {target} (owner: {owner}:{group})",
            file=sys.stderr,
        )
    except KeyError:
        print(f"  Wrote placeholder {target}", file=sys.stderr)
    print(
        "  NOTE: replace before `systemctl --user enable --now stormpulse`.",
        file=sys.stderr,
    )
    return target.resolve()


def prompt_docker_service(compose_path: Path) -> str:
    """Parse services from compose file, let user pick the default."""
    services = parse_service_names(compose_path)
    if services:
        print("  Services found:", file=sys.stderr)
        for i, name in enumerate(services, 1):
            print(f"    {i}. {name}", file=sys.stderr)
        default = services[0]
        while True:
            choice = prompt(
                "Default service for commands (e.g. docker_logs)",
                default=default,
            )
            if choice.isdigit() and 1 <= int(choice) <= len(services):
                return services[int(choice) - 1]
            if choice:
                return choice
    # No services parsed, manual entry
    while True:
        value = prompt("Default service for commands (e.g. web)")
        if value:
            return value
        print("  Service name cannot be empty", file=sys.stderr)


def prompt_env_file(project_dir: Path) -> Path | None:
    """Detect ``.env``, accept it, offer to create an empty one, or skip.

    On agent-first installs the project's ``.env`` often doesn't
    exist yet -- the operator will fill in ``KEY=value`` pairs when
    they install the project software. When ``project_dir`` is
    writable, offer to create an empty file so the wizard can move
    on without a manual ``touch``. Decline or unwritable dir falls
    through to the existing manual-entry / skip path.
    """
    env_path = project_dir / ".env"
    if env_path.is_file():
        choice = prompt(f"Found {env_path}. Use as env_file? (y/n/skip)", default="y")
        if choice.lower() in ("y", "yes", ""):
            return env_path
        if choice.lower() in ("n", "no", "skip", "s"):
            return None
    elif os.access(project_dir, os.W_OK):
        if prompt_confirm(f"  Create empty {env_path}?", default_yes=True):
            env_path.touch()
            try:
                st = env_path.stat()
                owner = pwd.getpwuid(st.st_uid).pw_name
                group = grp.getgrgid(st.st_gid).gr_name
                print(f"  Wrote {env_path} (owner: {owner}:{group})", file=sys.stderr)
            except KeyError:
                print(f"  Wrote {env_path}", file=sys.stderr)
            return env_path.resolve()
    choice = prompt("Path to .env file (or 'skip')", default="skip")
    if choice.lower() in ("skip", "s", ""):
        return None
    p = Path(choice)
    if p.is_file():
        return p.resolve()
    print(f"  Warning: {choice} not found. Writing path anyway.", file=sys.stderr)
    return Path(choice).resolve()
