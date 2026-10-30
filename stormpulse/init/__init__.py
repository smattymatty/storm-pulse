"""Interactive setup wizard, run after enrollment."""

from __future__ import annotations

from stormpulse.init.checks import (
    InitError,
    check_credentials,
    derive_dashboard_url,
    extract_agent_id,
    load_enroll_metadata,
    refuse_root,
)
from stormpulse.init.compose import (
    detect_compose_files,
    parse_service_names,
)
from stormpulse.init.files import (
    default_config_path,
    user_config_dir,
    user_config_path,
    user_data_dir,
    user_systemd_path,
    write_file,
    write_user_config_file,
    write_user_systemd_unit,
)
from stormpulse.init.generate import (
    TOML_TEMPLATE,
    InitConfig,
    generate_toml,
    render_systemd_unit,
)
from stormpulse.init.orchestrator import run_init
from stormpulse.init.prompts import (
    UUID_RE,
    prompt,
    prompt_compose_file,
    prompt_dashboard_url,
    prompt_docker_service,
    prompt_env_file,
    prompt_project_dir,
    prompt_pulse_token,
)
from stormpulse.init.system import (
    check_linger_enabled,
    run_cmd,
    run_system_setup,
    run_user_daemon_reload,
)

__all__ = [
    "InitConfig",
    "InitError",
    "TOML_TEMPLATE",
    "UUID_RE",
    "prompt",
    "run_cmd",
    "write_file",
    "check_credentials",
    "check_linger_enabled",
    "derive_dashboard_url",
    "detect_compose_files",
    "extract_agent_id",
    "generate_toml",
    "render_systemd_unit",
    "load_enroll_metadata",
    "parse_service_names",
    "prompt_compose_file",
    "prompt_dashboard_url",
    "prompt_docker_service",
    "prompt_env_file",
    "prompt_project_dir",
    "prompt_pulse_token",
    "refuse_root",
    "run_init",
    "run_system_setup",
    "run_user_daemon_reload",
    "default_config_path",
    "user_config_dir",
    "user_config_path",
    "user_data_dir",
    "user_systemd_path",
    "write_user_config_file",
    "write_user_systemd_unit",
]
