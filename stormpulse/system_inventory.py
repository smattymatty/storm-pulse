"""System inventory collector.

Reports OS, kernel, and key service versions to the dashboard on register,
and the node's dpkg source packages whenever dpkg changes. The dashboard persists this as ``SoftwareInstallation`` rows used for
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
from pathlib import Path
from typing import Any

from stormpulse.protocol import InventoryPackagesPayload, SourcePackage

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


_OS_RELEASE = Path('/etc/os-release')


def _read_os_release() -> dict[str, str]:
    """Parse /etc/os-release into a dict. Returns {} on any failure."""
    try:
        with open(_OS_RELEASE, encoding='utf-8') as f:
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
            argv,
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
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

        {"os": {"id": "debian", "version": "12", "codename": "bookworm",
                "pretty": "Debian GNU/Linux 12 (bookworm)"},
         "kernel": "6.1.0-26-amd64",
         "services": [{"name": "docker", "version": "24.0.2"}, ...]}
    """
    out: dict[str, Any] = {}

    os_release = _read_os_release()
    os_block: dict[str, str] = {}
    if os_release.get('ID'):
        os_block['id'] = os_release['ID']
    if os_release.get('VERSION_ID'):
        os_block['version'] = os_release['VERSION_ID']
    if os_release.get('VERSION_CODENAME'):
        os_block['codename'] = os_release['VERSION_CODENAME']
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


# One argv element, no shell: dpkg-query expands the fields itself.
_DPKG_FORMAT = (
    '${db:Status-Abbrev}\t${Package}\t${Version}'
    '\t${source:Package}\t${source:Version}\n'
)
# Mid-upgrade states: half-installed, unpacked, half-configured, triggers.
_UNSETTLED = frozenset('HUFWt')


def fold_dpkg_rows(text: str) -> list[SourcePackage] | None:
    """Fold ``dpkg-query`` rows to distinct (source, version) pairs, sorted.

    Keeps installed rows (status ``i``, no error flag; held ``hi`` stays).
    None when any row is unsettled, errored or malformed: the receiver prunes
    what a list omits, so a partial list is never sent.
    """
    pairs: set[tuple[str, str]] = set()
    for row in text.splitlines():
        if not row:
            continue
        fields = row.split('\t')
        if len(fields) != 5 or len(fields[0]) != 3:
            return None
        abbrev, _binary, _binary_version, source, version = fields
        if abbrev[1] in _UNSETTLED or abbrev[2] != ' ':
            return None
        if abbrev[1] == 'i':
            pairs.add((source.lower(), version))
    if not pairs:
        return None
    return [SourcePackage(name=n, version=v) for n, v in sorted(pairs)]


def _dpkg_query() -> str | None:
    """dpkg-query's rows in ``_DPKG_FORMAT``, or None when it cannot run cleanly."""
    try:
        res = subprocess.run(
            ['dpkg-query', '-W', '--showformat=' + _DPKG_FORMAT],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning('dpkg-query failed: %s', exc)
        return None
    if res.returncode != 0:
        logger.warning('dpkg-query exited %d', res.returncode)
        return None
    return res.stdout


def collect_source_packages() -> InventoryPackagesPayload | None:
    """The node's installed source packages under its release codename.

    None means send nothing: not a dpkg host, no codename, or a failed collect.
    Blocking; callers run it off the event loop.
    """
    release = _read_os_release().get('VERSION_CODENAME')
    if not release:
        return None
    rows = _dpkg_query()
    packages = fold_dpkg_rows(rows) if rows is not None else None
    if packages is None:
        logger.info('dpkg is unsettled or unreadable; packages not sent')
        return None
    return InventoryPackagesPayload(release=release, packages=packages)
