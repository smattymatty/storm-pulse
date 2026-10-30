"""Compose file detection and parsing helpers."""

from __future__ import annotations

import re
from pathlib import Path


def detect_compose_files(project_dir: Path) -> list[Path]:
    """Find candidate docker-compose files in a project directory."""
    candidates = [
        project_dir / "docker-compose.yml",
        project_dir / "docker-compose.yaml",
        project_dir / "docker" / "docker-compose.yml",
        project_dir / "docker" / "docker-compose.yaml",
        project_dir / "docker" / "docker-compose.prod.yml",
        project_dir / "docker" / "docker-compose.prod.yaml",
    ]
    return [p for p in candidates if p.is_file()]


def parse_service_names(compose_path: Path) -> list[str]:
    """Parse service names from a compose file (naive line-by-line).

    Looks for a top-level ``services:`` key, then collects lines with
    exactly 2-space indentation followed by a word and colon.
    """
    try:
        lines = compose_path.read_text("utf-8").splitlines()
    except OSError:
        return []

    in_services = False
    services: list[str] = []
    for line in lines:
        stripped = line.rstrip()
        if stripped == "" or stripped.startswith("#"):
            continue
        if not in_services:
            if re.match(r"^services:\s*$", stripped) or stripped == "services:":
                in_services = True
            continue
        # Inside services block
        if re.match(r"^\S", stripped):
            # Hit next top-level key, stop
            break
        m = re.match(r"^  ([a-zA-Z0-9_][\w-]*):\s*$", stripped)
        if m:
            services.append(m.group(1))
    return services
