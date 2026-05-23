"""Top-level ``run_init`` orchestrator."""

from __future__ import annotations

import sys
from pathlib import Path

from stormpulse.init.checks import (
    InitError,
    check_credentials,
    check_root,
    derive_dashboard_url,
    extract_agent_id,
    load_enroll_metadata,
)
from stormpulse.init.files import (
    CONFIG_PATH,
    SYSTEMD_PATH,
    write_config_file,
    write_systemd_unit,
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
from stormpulse.init.system import run_daemon_reload, run_system_setup


def run_init(creds_dir: Path, *, force: bool = False) -> None:
    """Public entry point for the init wizard."""
    check_root()
    check_credentials(creds_dir)

    agent_id = extract_agent_id(creds_dir)
    meta = load_enroll_metadata(creds_dir)

    print(f"\nStorm Pulse Init - configuring agent '{agent_id}'\n", file=sys.stderr)

    # Derive dashboard URL default from enrollment metadata
    dashboard_default: str | None = None
    if meta.get("endpoint"):
        dashboard_default = derive_dashboard_url(meta["endpoint"])

    pulse_token = prompt_pulse_token()
    dashboard_url = prompt_dashboard_url(default=dashboard_default)
    project_dir = prompt_project_dir()
    compose_file = prompt_compose_file(project_dir)
    docker_service_name = prompt_docker_service(compose_file)
    env_file = prompt_env_file(project_dir)

    config = InitConfig(
        agent_id=agent_id,
        pulse_token=pulse_token,
        dashboard_url=dashboard_url,
        creds_dir=creds_dir,
        project_dir=project_dir,
        compose_file=compose_file,
        docker_service_name=docker_service_name,
        env_file=env_file,
    )

    # Check for existing config
    if CONFIG_PATH.is_file() and not force:
        confirm = prompt(f"{CONFIG_PATH} already exists. Overwrite? (y/n)", default="n")
        if confirm.lower() not in ("y", "yes"):
            raise InitError("Aborted - config file not overwritten")
        force = True

    print("\nWriting files...", file=sys.stderr)
    write_config_file(CONFIG_PATH, generate_toml(config), force=force)
    print(f"  Config:  {CONFIG_PATH}", file=sys.stderr)

    write_systemd_unit(SYSTEMD_PATH, render_systemd_unit(project_dir), force=force)
    print(f"  Systemd: {SYSTEMD_PATH}", file=sys.stderr)

    # Feature install steps (Garage, logging, ...). Features register their
    # step with stormpulse.init.registry; the orchestrator runs them here
    # without importing any feature - the CORE-000 dependency inversion.
    for step in registered_init_steps():
        step(CONFIG_PATH)

    print("\nSystem setup...", file=sys.stderr)
    run_system_setup(project_dir, compose_file)
    run_daemon_reload()

    print(f"""
Setup complete!

Next steps:
  1. Set the git remote URL in {project_dir}:
     git -C {project_dir} remote set-url origin <HTTPS_URL>
  2. Start the agent:
     sudo systemctl enable --now stormpulse
  3. Check logs:
     sudo journalctl -u stormpulse -f
""", file=sys.stderr)
