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


def parse_volume_mounts(compose_path: Path, project_dir: Path) -> list[Path] | None:
    """Parse bind-mount volume directories from a compose file.

    Returns absolute paths for ``./relative:/container`` style mounts.
    Named volumes (no ``./`` prefix) are ignored.

    Returns ``None`` on parse failure (file unreadable or not a compose file)
    so callers can distinguish "no bind mounts" from "couldn't parse."
    """
    try:
        lines = compose_path.read_text("utf-8").splitlines()
    except OSError:
        return None

    # Sanity check: must have a top-level services: line
    if not any(re.match(r"^services:\s*$", line.rstrip()) for line in lines):
        return None

    volumes: list[Path] = []
    in_volumes = False
    for line in lines:
        stripped = line.rstrip()
        if stripped == "" or stripped.startswith("#"):
            continue
        # Detect a volumes: block (at any service-level indentation)
        if re.match(r"^\s+volumes:\s*$", stripped):
            in_volumes = True
            continue
        if in_volumes:
            # Volume list items start with "- " after indentation
            m = re.match(r"""^\s+- ["']?(\./[^:"']+)["']?:""", stripped)
            if m:
                host_path = m.group(1)
                resolved = (project_dir / host_path).resolve()
                if resolved not in volumes:
                    volumes.append(resolved)
                continue
            # If line is not a list item, check if we left the volumes block
            if re.match(r"^\s+\S", stripped) and not stripped.lstrip().startswith("- "):
                in_volumes = False
    return volumes
