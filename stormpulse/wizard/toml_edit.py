"""Generic, host-owned TOML section editing for the wizard engine.

Framework layer. The engine claims an integration's *own* ``[section]`` in
``stormpulse.toml``; it never runs arbitrary TOML writes. The render is
deliberately minimal (v1 scalars only) and byte-compatible with the existing
feature init templates, so porting a wizard produces identical config.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

from stormpulse.sdk import TomlScalar
from stormpulse.wizard.errors import WizardError

_SECTION_HEADER = re.compile(r"^\[")


def render_scalar(value: TomlScalar) -> str:
    """Render a v1 TOML scalar. ``bool`` is checked before ``int`` (bool is an int)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    raise WizardError(f"unsupported TOML scalar type: {type(value).__name__}")


def render_section(section: str, content: dict[str, TomlScalar]) -> str:
    """Render ``[section]`` with a leading blank line and a trailing newline, the
    shape the feature init templates use (so a port is byte-identical)."""
    lines = [f"\n[{section}]"]
    lines.extend(f"{key} = {render_scalar(value)}" for key, value in content.items())
    return "\n".join(lines) + "\n"


def remove_section(lines: list[str], section: str) -> list[str]:
    """Drop ``[section]`` (line-based) from TOML lines, preserving everything else
    and a preceding blank line, mirroring the feature ``remove_*_section`` helpers."""
    result: list[str] = []
    header = f"[{section}]"
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped == header:
            in_section = True
            if result and result[-1].strip() == "":
                result.pop()
            continue
        if in_section:
            if _SECTION_HEADER.match(stripped):
                in_section = False
                result.append(line)
            continue
        result.append(line)
    return result


def read_bytes_or_none(path: Path) -> bytes | None:
    """Current file bytes, or ``None`` if it does not exist (pre-image capture)."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write ``data`` to ``path`` via temp file + fsync + atomic replace + dir fsync."""
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with open(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    os.chmod(path, mode)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def restore_or_remove(path: Path, pre_image: bytes | None, mode: int = 0o644) -> None:
    """Compensation primitive: restore captured bytes, or remove a file that did
    not exist before the mutation."""
    if pre_image is None:
        path.unlink(missing_ok=True)
        return
    atomic_write_bytes(path, pre_image, mode)


def claim_section(config_path: Path, section: str, content: dict[str, TomlScalar]) -> None:
    """Create or replace the integration's own ``[section]`` in ``config_path``."""
    text = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    kept = remove_section(text.splitlines(keepends=True), section)
    new_text = "".join(kept) + render_section(section, content)
    atomic_write_bytes(config_path, new_text.encode("utf-8"))


def section_equals(config_path: Path, section: str, content: dict[str, TomlScalar]) -> bool:
    """Whether ``config_path`` parses and its ``[section]`` equals ``content``."""
    try:
        parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return parsed.get(section) == content
