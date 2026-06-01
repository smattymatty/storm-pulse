"""System setup commands (chown, docker group, systemd reload, restart)."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from stormpulse.init.checks import InitError
from stormpulse.init.compose import parse_volume_mounts
from stormpulse.init.mode import InstallMode

logger = logging.getLogger("stormpulse")


def run_cmd(args: list[str], *, description: str) -> bool:
    """Run a system command, printing status. Returns True on success."""
    print(f"  {description}...", file=sys.stderr)
    try:
        subprocess.run(args, check=True, capture_output=True)
        return True
    except FileNotFoundError:
        print(f"    Command not found: {args[0]}", file=sys.stderr)
        return False
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace").strip()
        print(f"    Failed: {stderr or exc}", file=sys.stderr)
        return False


def run_find_apply(
    root: Path,
    exclude: list[Path],
    cmd_args: list[str],
    *,
    description: str,
) -> bool:
    """Run ``find <root> -prune ... -print0 | xargs -0 <cmd>``.

    Excludes directories in *exclude* from traversal using ``-prune``,
    so they are never touched by *cmd_args*.
    """
    print(f"  {description}...", file=sys.stderr)
    find_args: list[str] = ["/usr/bin/find", str(root)]
    for excl in exclude:
        find_args += ["-path", str(excl), "-prune", "-o"]
    find_args += ["-print0"]

    try:
        find_proc = subprocess.Popen(
            find_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        xargs_proc = subprocess.Popen(
            ["/usr/bin/xargs", "-0", *cmd_args],
            stdin=find_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if find_proc.stdout:
            find_proc.stdout.close()
        _, xargs_stderr = xargs_proc.communicate()
        find_proc.wait()
        if find_proc.returncode != 0 or xargs_proc.returncode != 0:
            err = xargs_stderr.decode("utf-8", errors="replace").strip()
            print(f"    Failed: {err or 'non-zero exit'}", file=sys.stderr)
            return False
        return True
    except FileNotFoundError as exc:
        print(f"    Command not found: {exc.filename}", file=sys.stderr)
        return False
    except OSError as exc:
        print(f"    Failed: {exc}", file=sys.stderr)
        return False


def run_system_setup(
    project_dir: Path,
    compose_file: Path,
    mode: InstallMode = InstallMode.SYSTEM,
) -> None:
    """Best-effort system setup: docker group, git safe.directory, permissions.

    In ``USER`` mode, all the steps that need root (usermod, system
    git safe.directory, recursive chown root:stormpulse) are skipped
    because the agent runs as the unprivileged user that already owns
    the project directory and has no need to join a docker group
    (rootless docker is per-user). See ADR CORE-003.
    """
    if mode is InstallMode.USER:
        # User mode: the agent runs as the operator. The operator
        # already owns project_dir, already reaches rootless docker via
        # its per-user socket, and doesn't need a system-wide git
        # safe.directory rule. Set a user-scoped git safe.directory in
        # case the agent does git operations from a unit it didn't
        # check out itself.
        run_cmd(
            [
                "/usr/bin/git",
                "config",
                "--global",
                "--add",
                "safe.directory",
                str(project_dir),
            ],
            description=f"Marking {project_dir} as git safe.directory (user)",
        )
        return

    run_cmd(
        ["/usr/sbin/usermod", "-aG", "docker", "stormpulse"],
        description="Adding stormpulse to docker group",
    )
    run_cmd(
        [
            "/usr/bin/git",
            "config",
            "--system",
            "--add",
            "safe.directory",
            str(project_dir),
        ],
        description=f"Marking {project_dir} as git safe.directory",
    )

    # Safe recursive chown with volume exclusion
    volume_dirs = parse_volume_mounts(compose_file, project_dir)

    if volume_dirs is None:
        print(
            f"  WARNING: Could not parse {compose_file} for volume mounts.\n"
            f"    Skipping recursive chown to avoid breaking Docker volumes.\n"
            f"    Set ownership manually: chown -R root:stormpulse {project_dir}",
            file=sys.stderr,
        )
    elif volume_dirs:
        if not run_find_apply(
            project_dir,
            volume_dirs,
            ["/usr/bin/chown", "root:stormpulse"],
            description=f"chown root:stormpulse {project_dir} (excluding {len(volume_dirs)} volume(s))",
        ):
            print("    Cannot set project directory permissions.", file=sys.stderr)
            return
        run_find_apply(
            project_dir,
            volume_dirs,
            ["/usr/bin/chmod", "g+w"],
            description=f"chmod g+w {project_dir} (excluding {len(volume_dirs)} volume(s))",
        )
        existing = [d for d in volume_dirs if d.is_dir()]
        if existing:
            print(
                f"  Excluded {len(existing)} Docker volume(s) from ownership changes.",
                file=sys.stderr,
            )
    else:
        if not run_cmd(
            ["/usr/bin/chown", "-R", "root:stormpulse", str(project_dir)],
            description=f"chown -R root:stormpulse {project_dir}",
        ):
            print("    Cannot set project directory permissions.", file=sys.stderr)
            return
        run_cmd(
            ["/usr/bin/chmod", "-R", "g+w", str(project_dir)],
            description=f"chmod -R g+w {project_dir}",
        )


def run_daemon_reload() -> None:
    """Reload systemd to pick up the new unit file."""
    if not run_cmd(
        ["/usr/bin/systemctl", "daemon-reload"],
        description="Reloading systemd",
    ):
        raise InitError("systemctl daemon-reload failed")


def run_user_daemon_reload() -> None:
    """Reload the user systemd to pick up the new user unit file."""
    if not run_cmd(
        ["/usr/bin/systemctl", "--user", "daemon-reload"],
        description="Reloading user systemd",
    ):
        raise InitError("systemctl --user daemon-reload failed")


def check_linger_enabled() -> bool:
    """Return True if the current user has linger enabled.

    User systemd units only survive logout when ``loginctl
    enable-linger`` is set for the user. Without linger, the unit
    stops when the operator logs out -- which makes the agent useless.
    """
    import os

    user = os.environ.get("USER") or ""
    if not user:
        return False
    try:
        result = subprocess.run(
            ["/usr/bin/loginctl", "show-user", user, "--property=Linger"],
            capture_output=True,
            text=True,
            check=False,
        )
        return "Linger=yes" in result.stdout
    except (FileNotFoundError, OSError):
        return False


def _restart_user_unit() -> int:
    """Run ``systemctl --user restart stormpulse``. Return its exit code.

    Prints a status line before invoking and a success/tail line on
    return when the unit restarted cleanly. Errors are logged via
    ``logger.error`` so they surface in the same journal stream as
    the agent itself.
    """
    print("Restarting user unit: stormpulse...", file=sys.stderr)
    result = subprocess.run(["systemctl", "--user", "restart", "stormpulse"])
    if result.returncode != 0:
        logger.error(
            "systemctl --user restart stormpulse exited %d",
            result.returncode,
        )
        return result.returncode
    print(
        "Restarted. Tail with: stormpulse logs",
        file=sys.stderr,
    )
    return 0


def _print_system_restart_hint() -> None:
    """Print the system-mode restart instruction without executing.

    Matches the no-escalation posture: the wrapper never shells out
    to sudo. The operator runs the command in a context that already
    holds the privilege.
    """
    print(
        "System install detected. Restart when ready: systemctl restart stormpulse",
        file=sys.stderr,
    )


def restart_or_hint(mode: InstallMode) -> int:
    """Restart the unit (user mode) or print the hint (system mode).

    Returns the exit code the caller should propagate. ``0`` when the
    system-mode hint was printed (the hint is informational, not a
    failure) or when the user-mode restart succeeded.
    """
    if mode is InstallMode.SYSTEM:
        _print_system_restart_hint()
        return 0
    return _restart_user_unit()
