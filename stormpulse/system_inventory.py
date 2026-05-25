"""System inventory collector.

Reports OS, kernel, and key service versions to the dashboard on register.
The dashboard persists this as ``SoftwareInstallation`` rows used for
asset management and CVE matching.

Collection is best-effort: every probe wraps its own failure so a missing
binary or unreadable file does not block register.
"""

from __future__ import annotations

import logging
import platform
import re
import shutil
import subprocess
from typing import Any

logger = logging.getLogger(__name__)


# Services Storm cares about. Each entry is the binary name and the
# command-line flag that prints something containing the version.
_SERVICE_PROBES: list[tuple[str, list[str]]] = [
    ('docker', ['docker', '--version']),
    ('caddy', ['caddy', 'version']),
    ('garage', ['garage', '--version']),
    ('fail2ban', ['fail2ban-server', '--version']),
]

# Match the first version-shaped token in a probe's output:
#   docker version 24.0.2, build ...        -> 24.0.2
#   v2.7.6 h1:...                           -> 2.7.6
#   garage 1.0.1 [features: ...]            -> 1.0.1
#   Fail2Ban v1.0.2                         -> 1.0.2
_VERSION_RE = re.compile(r'(\d+\.\d+(?:\.\d+)?(?:[-+][\w.]+)?)')


def _read_os_release() -> dict[str, str]:
    """Parse /etc/os-release into a dict. Returns {} on any failure."""
    try:
        with open('/etc/os-release', encoding='utf-8') as f:
            raw = f.read()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in raw.splitlines():
        if '=' not in line or line.startswith('#'):
            continue
        key, _, value = line.partition('=')
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _probe_service(binary: str, argv: list[str]) -> str | None:
    """Run argv and pull the first version-shaped token from stdout+stderr.

    Returns None when the binary is not on PATH or the probe fails or
    nothing version-shaped appears in the output.
    """
    if shutil.which(binary) is None:
        return None
    try:
        res = subprocess.run(
            argv, capture_output=True, text=True, timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug('Service probe %s failed: %s', binary, exc)
        return None
    text = (res.stdout or '') + ' ' + (res.stderr or '')
    m = _VERSION_RE.search(text)
    return m.group(1) if m else None


def collect_system_inventory() -> dict[str, Any]:
    """Build the system_inventory payload for the register envelope.

    Shape (every field is optional - the dashboard accepts a partial dict):

        {
          "os":     {"id": "ubuntu", "version": "24.04", "pretty": "Ubuntu 24.04 LTS"},
          "kernel": "6.8.0-40-generic",
          "services": [{"name": "docker", "version": "24.0.2"}, ...],
        }
    """
    out: dict[str, Any] = {}

    os_release = _read_os_release()
    os_block: dict[str, str] = {}
    if os_release.get('ID'):
        os_block['id'] = os_release['ID']
    if os_release.get('VERSION_ID'):
        os_block['version'] = os_release['VERSION_ID']
    if os_release.get('PRETTY_NAME'):
        os_block['pretty'] = os_release['PRETTY_NAME']
    if os_block:
        out['os'] = os_block

    try:
        kernel = platform.release()
    except Exception:
        kernel = ''
    if kernel:
        out['kernel'] = kernel

    services: list[dict[str, str]] = []
    for name, argv in _SERVICE_PROBES:
        version = _probe_service(argv[0], argv)
        if version:
            services.append({'name': name, 'version': version})
    if services:
        out['services'] = services

    return out
