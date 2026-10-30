"""Top-level ``run_init`` orchestrator."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from stormpulse.init.checks import (
    InitError,
    check_credentials,
    derive_dashboard_url,
    extract_agent_id,
    load_enroll_metadata,
)
from stormpulse.init.files import (
    user_config_path,
    user_data_dir,
    user_systemd_path,
    write_user_config_file,
    write_user_systemd_unit,
)
from stormpulse.init.generate import InitConfig, generate_toml, render_systemd_unit
from stormpulse.init.prompts import (
    prompt,
    prompt_compose_file,
    prompt_dashboard_url,
    prompt_docker_service,
    prompt_env_file,
    prompt_project_dir,
    prompt_pulse_token,
)
from stormpulse.init.registry import registered_init_steps
from stormpulse.init.system import (
    check_linger_enabled,
    run_system_setup,
    run_user_daemon_reload,
)


def run_init(creds_dir: Path, *, force: bool = False) -> None:
    """Public entry point for the init wizard (ADR CORE-003)."""
    check_credentials(creds_dir)

    config_path = user_config_path()
    config = _prompt_config(creds_dir, config_path)

    if config_path.is_file() and not force:
        confirm = prompt(f"{config_path} already exists. Overwrite? (y/n)", default="n")
        if confirm.lower() not in ("y", "yes"):
            raise InitError("Aborted - config file not overwritten")
        force = True

    _write_files(config, config_path, force=force)
    _setup_host(config.project_dir)


def _prompt_config(creds_dir: Path, config_path: Path) -> InitConfig:
    """Ask the wizard questions; ``config_path`` recalls a previous pulse token."""
    agent_id = extract_agent_id(creds_dir)
    meta = load_enroll_metadata(creds_dir)

    print(
        f"\nStorm Pulse Init - configuring agent '{agent_id}'\n",
        file=sys.stderr,
    )

    # Prefer the dashboard's explicit transport boundary. Enrollment responses
    # from older dashboards lack it, so preserve the historical derivation.
    dashboard_default = meta.get("dashboard_url")
    if dashboard_default is None and meta.get("endpoint"):
        dashboard_default = derive_dashboard_url(meta["endpoint"])

    pulse_token = prompt_pulse_token(remembered_from=config_path)
    dashboard_url = prompt_dashboard_url(default=dashboard_default)
    project_dir = prompt_project_dir()
    compose_file = prompt_compose_file(project_dir)

    return InitConfig(
        agent_id=agent_id,
        pulse_token=pulse_token,
        dashboard_url=dashboard_url,
        creds_dir=creds_dir,
        project_dir=project_dir,
        compose_file=compose_file,
        docker_service_name=prompt_docker_service(compose_file),
        env_file=prompt_env_file(project_dir),
        data_dir=user_data_dir(),
    )


def _write_files(config: InitConfig, config_path: Path, *, force: bool) -> None:
    print("\nWriting files...", file=sys.stderr)
    write_user_config_file(config_path, generate_toml(config), force=force)
    print(f"  Config:  {config_path}", file=sys.stderr)

    systemd_path = user_systemd_path()
    agent_bin = _resolve_user_agent_bin()
    user_data_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    unit_content = render_systemd_unit(agent_bin, config_path)
    write_user_systemd_unit(systemd_path, unit_content, force=force)
    print(f"  Systemd: {systemd_path}", file=sys.stderr)

    # Feature install steps (Garage, logging, signoff, ...). Features
    # register their step with stormpulse.init.registry; the orchestrator
    # runs them here without importing any feature - the CORE-000
    # dependency inversion.
    for step in registered_init_steps():
        step(config_path)


def _setup_host(project_dir: Path) -> None:
    print("\nSystem setup...", file=sys.stderr)
    run_system_setup(project_dir)
    run_user_daemon_reload()
    if not check_linger_enabled():
        print(
            "\n  WARNING: linger is NOT enabled for this user. The agent\n"
            "  will stop when you log out. Enable with:\n"
            "    sudo loginctl enable-linger $USER\n",
            file=sys.stderr,
        )

    print(
        f"""
Setup complete!

Next steps:
  1. Set the git remote URL in {project_dir}:
     git -C {project_dir} remote set-url origin <HTTPS_URL>
  2. Start the agent:
     systemctl --user enable --now stormpulse
  3. Check logs:
     stormpulse logs
""",
        file=sys.stderr,
    )


def _resolve_user_agent_bin() -> Path:
    """Locate the stormpulse binary for the user systemd unit.

    Prefers `~/.local/bin/stormpulse` (pipx default) and falls back to
    whatever's on PATH. The unit needs an absolute path; PATH lookups
    don't apply inside systemd.
    """
    pipx_bin = Path.home() / ".local" / "bin" / "stormpulse"
    if pipx_bin.is_file():
        return pipx_bin
    which = shutil.which("stormpulse")
    if which:
        return Path(which)
    raise InitError(
        "Cannot locate the 'stormpulse' binary. Install with "
        "'pipx install storm-pulse-agent' (or equivalent) and rerun.",
    )
