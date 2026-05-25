"""Migrate an existing rootful Pulse install to user-mode rootless.

See ADR CORE-003. The migration is in-place: the agent's cryptographic
identity (EC keypair, signed cert, CA cert, HMAC secret) is preserved
on disk, so the dashboard treats the migrated agent as the same
server.

Steps (in order):

1. Sanity-check the box: refuse if rootless docker isn't usable.
2. Stop the system unit via sudo so cred files aren't moving while
   the agent might be reading them.
3. Copy the four cred files from ``/etc/stormpulse/`` to
   ``~/.config/stormpulse/`` and re-chown to the current user.
4. Rewrite the TOML so ``[tls]``, ``[auth]``, and ``[storage]`` paths
   point at user-scoped locations.
5. Write a user systemd unit and enable+start it via
   ``systemctl --user``.
6. Verify the new unit is active. On failure, the operator can fall
   back by re-enabling the old system unit because the old files
   were left in place.

The optional cleanup of the old system install (``/etc/stormpulse/``,
``/opt/stormpulse/``, the ``stormpulse`` system user) is offered as a
final step but defaulted to NO so the rollback path stays open.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from stormpulse.init.checks import InitError
from stormpulse.init.files import (
    CONFIG_PATH,
    SYSTEMD_PATH,
    user_config_dir,
    user_config_path,
    user_data_dir,
    user_systemd_path,
    write_user_config_file,
    write_user_systemd_unit,
)
from stormpulse.init.generate import USER_SYSTEMD_UNIT_TEMPLATE
from stormpulse.init.mode import InstallMode, rootless_socket_path

OLD_CREDS_DIR = Path("/etc/stormpulse")
CRED_FILES = ("agent.pem", "agent-key.pem", "ca.pem", "hmac.key")


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """Resolved set of paths the migration will operate on."""
    old_config: Path
    old_systemd: Path
    old_creds_dir: Path
    new_config: Path
    new_systemd: Path
    new_creds_dir: Path
    new_data_dir: Path


def build_plan() -> MigrationPlan:
    return MigrationPlan(
        old_config=CONFIG_PATH,
        old_systemd=SYSTEMD_PATH,
        old_creds_dir=OLD_CREDS_DIR,
        new_config=user_config_path(),
        new_systemd=user_systemd_path(),
        new_creds_dir=user_config_dir(),
        new_data_dir=user_data_dir(),
    )


def check_preconditions(plan: MigrationPlan) -> None:
    """Refuse to migrate when the environment isn't ready.

    Bails (rather than papers over) when:
    - The current process is root (the user unit must be owned by the
      unprivileged user)
    - Rootless docker isn't running
    - The old system install isn't present (nothing to migrate from)
    - The new user-mode files already exist (migration ran already)
    """
    if os.geteuid() == 0:
        raise InitError(
            "Run 'stormpulse migrate-to-rootless' as your own user, "
            "not via sudo. The migration uses sudo only for the few "
            "steps that need it.",
        )
    sock = rootless_socket_path()
    if sock is None or not sock.exists():
        raise InitError(
            "No rootless docker socket detected at "
            f"$XDG_RUNTIME_DIR/docker.sock. Migrate the box to "
            "rootless docker first (see playbook 001-ubuntu-baseline).",
        )
    if not plan.old_config.is_file():
        raise InitError(
            f"No existing rootful install found at {plan.old_config}. "
            "Nothing to migrate. Use 'stormpulse init --user' for a "
            "fresh rootless install.",
        )
    if plan.new_config.is_file():
        raise InitError(
            f"User-mode install already exists at {plan.new_config}. "
            "Migration appears to have run already. Use --force to "
            "redo it (this OVERWRITES the user-mode files).",
        )


def stop_system_unit() -> None:
    """sudo systemctl disable --now stormpulse. Prompts for password."""
    print("Stopping existing system unit (sudo required)...", file=sys.stderr)
    result = subprocess.run(
        ["sudo", "systemctl", "disable", "--now", "stormpulse"],
        check=False,
    )
    if result.returncode != 0:
        raise InitError(
            "Could not stop the existing system unit. Check that "
            "sudo works for your user, then retry.",
        )


def copy_creds(plan: MigrationPlan, *, force: bool = False) -> None:
    """Copy cred files from /etc/stormpulse to ~/.config/stormpulse.

    Uses sudo to read /etc/stormpulse (root-owned, group=stormpulse,
    mode 0640). Then chowns to the invoking user and tightens
    permissions to user-only.
    """
    plan.new_creds_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    user = os.environ.get("USER") or ""
    if not user:
        raise InitError("Cannot determine $USER for chown after copy.")

    print(f"Copying credentials to {plan.new_creds_dir} (sudo required)...",
          file=sys.stderr)
    for name in CRED_FILES:
        src = plan.old_creds_dir / name
        dst = plan.new_creds_dir / name
        if not src.is_file():
            raise InitError(
                f"Missing credential file in old install: {src}. "
                "Re-enroll instead of migrating.",
            )
        if dst.is_file() and not force:
            raise InitError(
                f"Destination {dst} already exists. Use --force to "
                "overwrite (this overwrites pre-existing user-mode creds).",
            )
        # Copy via sudo, then re-own to the user. cp -p preserves mode
        # so we tighten after.
        cp = subprocess.run(
            ["sudo", "cp", "-p", str(src), str(dst)], check=False,
        )
        if cp.returncode != 0:
            raise InitError(f"sudo cp failed for {src} -> {dst}")
        chown = subprocess.run(
            ["sudo", "chown", f"{user}:{user}", str(dst)], check=False,
        )
        if chown.returncode != 0:
            raise InitError(f"sudo chown failed for {dst}")
        # 0600 for private key + hmac; 0644 for the public certs.
        mode = 0o600 if name in ("agent-key.pem", "hmac.key") else 0o644
        dst.chmod(mode)


# Path-replacement patterns for translating system TOML into user TOML.
# Order matters: longer match first to avoid '/etc/stormpulse' eating
# the prefix of '/etc/stormpulse/agent.pem'.
_PATH_TRANSLATIONS_TEMPLATE: tuple[tuple[str, str], ...] = (
    (str(OLD_CREDS_DIR) + "/agent-key.pem", "{creds}/agent-key.pem"),
    (str(OLD_CREDS_DIR) + "/agent.pem",     "{creds}/agent.pem"),
    (str(OLD_CREDS_DIR) + "/ca.pem",        "{creds}/ca.pem"),
    (str(OLD_CREDS_DIR) + "/hmac.key",      "{creds}/hmac.key"),
    ("/opt/stormpulse/data/stormpulse.db",  "{data}/stormpulse.db"),
)


def translate_toml(content: str, plan: MigrationPlan) -> str:
    """Rewrite the TOML so paths point at user-scoped locations."""
    creds = str(plan.new_creds_dir)
    data = str(plan.new_data_dir)
    out = content
    for old, new_template in _PATH_TRANSLATIONS_TEMPLATE:
        new = new_template.format(creds=creds, data=data)
        out = out.replace(old, new)
    return out


def write_user_toml(plan: MigrationPlan, *, force: bool = False) -> None:
    """Read the old TOML via sudo, translate paths, write the new one."""
    print("Translating TOML to user-mode paths...", file=sys.stderr)
    old_text = _sudo_read_text(plan.old_config)
    new_text = translate_toml(old_text, plan)
    plan.new_data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_user_config_file(plan.new_config, new_text, force=force)


def write_user_unit(plan: MigrationPlan, *, force: bool = False) -> None:
    """Generate and write the user systemd unit."""
    print("Writing user systemd unit...", file=sys.stderr)
    pipx_bin = Path.home() / ".local" / "bin" / "stormpulse"
    if pipx_bin.is_file():
        agent_bin = pipx_bin
    else:
        which = shutil.which("stormpulse")
        if not which:
            raise InitError(
                "Cannot locate the 'stormpulse' binary in ~/.local/bin "
                "or on PATH. Install via 'pipx install storm-pulse-agent' "
                "before migrating.",
            )
        agent_bin = Path(which)
    unit_content = (
        USER_SYSTEMD_UNIT_TEMPLATE
        .replace("{agent_bin}", str(agent_bin))
        .replace("{config_path}", str(plan.new_config))
    )
    write_user_systemd_unit(plan.new_systemd, unit_content, force=force)


def enable_and_start(plan: MigrationPlan) -> None:
    """systemctl --user daemon-reload && enable --now."""
    print("Enabling + starting user unit...", file=sys.stderr)
    for argv, desc in (
        (["systemctl", "--user", "daemon-reload"], "daemon-reload"),
        (["systemctl", "--user", "enable", "--now", "stormpulse"], "enable + start"),
    ):
        result = subprocess.run(argv, check=False)
        if result.returncode != 0:
            raise InitError(
                f"systemctl --user {desc} failed. The old system unit "
                "has been disabled; re-enable it with "
                "'sudo systemctl enable --now stormpulse' if you need "
                "to roll back.",
            )


def verify(plan: MigrationPlan) -> None:
    """Confirm the user unit is active."""
    print("Verifying user unit is active...", file=sys.stderr)
    result = subprocess.run(
        ["systemctl", "--user", "is-active", "stormpulse"],
        capture_output=True, text=True, check=False,
    )
    if result.stdout.strip() != "active":
        raise InitError(
            "User unit is not active after start. Check logs with "
            "'journalctl --user -u stormpulse -n 50' and roll back to "
            "the system unit if needed: "
            "'sudo systemctl enable --now stormpulse'.",
        )


def run_migration(*, force: bool = False) -> None:
    """Public entry point. Composes the steps in order."""
    plan = build_plan()
    check_preconditions(plan)
    stop_system_unit()
    try:
        copy_creds(plan, force=force)
        write_user_toml(plan, force=force)
        write_user_unit(plan, force=force)
        enable_and_start(plan)
        verify(plan)
    except Exception:
        # Best-effort rollback: leave the old install in place and tell
        # the operator how to re-enable it. We don't try to undo the
        # partial file writes because they're harmless if the system
        # unit is restarted.
        print(
            "\nMigration failed mid-way. Roll back with:\n"
            "  sudo systemctl enable --now stormpulse\n"
            "Then investigate; the user-mode files under "
            f"{plan.new_creds_dir} can be removed if you want a clean "
            "retry.",
            file=sys.stderr,
        )
        raise
    print(
        "\nMigration complete. The agent is now running as a user "
        "systemd unit.\n"
        "Old install left in place for rollback. Once you've confirmed "
        "the new agent is healthy in the dashboard, remove the old "
        "files with:\n"
        "  sudo systemctl disable stormpulse  # already done\n"
        "  sudo rm -rf /etc/stormpulse /opt/stormpulse\n"
        "  sudo userdel stormpulse\n",
        file=sys.stderr,
    )


def _sudo_read_text(path: Path) -> str:
    """Read a root-owned file via sudo cat."""
    result = subprocess.run(
        ["sudo", "cat", str(path)],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise InitError(f"sudo cat {path} failed: {result.stderr.strip()}")
    return result.stdout


# Exported for tests + later wiring; keeps the module surface explicit.
__all__ = [
    "CRED_FILES",
    "MigrationPlan",
    "build_plan",
    "check_preconditions",
    "copy_creds",
    "enable_and_start",
    "run_migration",
    "stop_system_unit",
    "translate_toml",
    "verify",
    "write_user_toml",
    "write_user_unit",
]
