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
    _CONFIG_PATH,
    _SYSTEMD_PATH,
    write_config_file,
    write_systemd_unit,
)
from stormpulse.init.generate import InitConfig, generate_toml, render_systemd_unit
from stormpulse.init.prompts import (
    _prompt,
    prompt_compose_file,
    prompt_dashboard_url,
    prompt_docker_service,
    prompt_env_file,
    prompt_project_dir,
    prompt_pulse_token,
)
from stormpulse.init.system import run_daemon_reload, run_system_setup


def run_init(creds_dir: Path, *, force: bool = False) -> None:
    """Public entry point for the init wizard."""
    check_root()
    check_credentials(creds_dir)

    agent_id = extract_agent_id(creds_dir)
    meta = load_enroll_metadata(creds_dir)

    print(f"\nStorm Pulse Init — configuring agent '{agent_id}'\n", file=sys.stderr)

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

    # Garage auto-detection
    garage_section = ""
    print("\nChecking for Garage installation...", file=sys.stderr)
    from stormpulse.garage.init import (
        find_garage_config,
        parse_garage_container_name,
        prompt_confirm,
        prompt_garage_values,
    )
    garage_config = find_garage_config()
    if garage_config:
        print(f"  Found: {garage_config}", file=sys.stderr)
        if prompt_confirm("\nEnable Garage integration?"):
            # Detect container name
            garage_dir = garage_config.parent
            container = "garaged"
            for name in ("docker-compose.yml", "docker-compose.yaml"):
                cp = garage_dir / name
                if cp.is_file():
                    container = parse_garage_container_name(cp)
                    break
            values = prompt_garage_values(
                container_name=container,
                garage_config_path=str(garage_config),
            )
            from stormpulse.garage.init import _GARAGE_TOML_TEMPLATE
            garage_section = _GARAGE_TOML_TEMPLATE.format(
                container_name=values["container_name"],
                garage_binary=values["garage_binary"],
                docker_binary=values["docker_binary"],
                config_path=values["garage_config_path"],
                state_push_interval_seconds=values["state_push_interval_seconds"],
            )
    else:
        print("  No Garage installation found. Skipping.", file=sys.stderr)

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
    if _CONFIG_PATH.is_file() and not force:
        confirm = _prompt(f"{_CONFIG_PATH} already exists. Overwrite? (y/n)", default="n")
        if confirm.lower() not in ("y", "yes"):
            raise InitError("Aborted — config file not overwritten")
        force = True

    toml_content = generate_toml(config) + garage_section
    print("\nWriting files...", file=sys.stderr)
    write_config_file(_CONFIG_PATH, toml_content, force=force)
    print(f"  Config:  {_CONFIG_PATH}", file=sys.stderr)

    write_systemd_unit(_SYSTEMD_PATH, render_systemd_unit(project_dir), force=force)
    print(f"  Systemd: {_SYSTEMD_PATH}", file=sys.stderr)

    # Logging auto-detection (Docker containers)
    print("\nChecking for log sources...", file=sys.stderr)
    from stormpulse.logging.init import (
        append_log_groups,
        detect_docker_containers,
        prompt_logging_setup,
    )
    containers = detect_docker_containers()
    if containers:
        print(
            f"  Found {len(containers)} running container(s): {', '.join(containers)}",
            file=sys.stderr,
        )
        log_groups = prompt_logging_setup(containers, existing_groups=[])
        if log_groups:
            append_log_groups(_CONFIG_PATH, log_groups)
            for g in log_groups:
                print(f"  Added: {g['name']} (docker)", file=sys.stderr)
    else:
        print("  No running containers found. Skipping.", file=sys.stderr)

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
