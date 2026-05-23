"""System setup commands (chown, docker group, systemd reload)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from stormpulse.init.checks import InitError
from stormpulse.init.compose import parse_volume_mounts


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
            find_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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
) -> None:
    """Best-effort system setup: docker group, git safe.directory, permissions."""
    run_cmd(
        ["/usr/sbin/usermod", "-aG", "docker", "stormpulse"],
        description="Adding stormpulse to docker group",
    )
    run_cmd(
        ["/usr/bin/git", "config", "--system", "--add", "safe.directory", str(project_dir)],
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
            project_dir, volume_dirs,
            ["/usr/bin/chown", "root:stormpulse"],
            description=f"chown root:stormpulse {project_dir} (excluding {len(volume_dirs)} volume(s))",
        ):
            print("    Cannot set project directory permissions.", file=sys.stderr)
            return
        run_find_apply(
            project_dir, volume_dirs,
            ["/usr/bin/chmod", "g+w"],
            description=f"chmod g+w {project_dir} (excluding {len(volume_dirs)} volume(s))",
        )
        existing = [d for d in volume_dirs if d.is_dir()]
        if existing:
            print(f"  Excluded {len(existing)} Docker volume(s) from ownership changes.", file=sys.stderr)
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


def restart_stormpulse() -> bool:
    """Restart the stormpulse systemd service. Returns True on success."""
    try:
        subprocess.run(
            ["/usr/bin/systemctl", "restart", "stormpulse"],
            check=True,
            capture_output=True,
        )
        return True
    except FileNotFoundError:
        print("  systemctl not found", file=sys.stderr)
        return False
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode("utf-8", errors="replace").strip()
        print(f"  Restart failed: {stderr or exc}", file=sys.stderr)
        return False
