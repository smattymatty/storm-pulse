"""Sign-off seal state for the dashboard verify-block hatch.

The agent registers ``run_verify_block`` so the Storm Developments
dashboard can dispatch HMAC-signed verify shell at signoff time (see
``commands/registry.py``). That capability is intentionally wide: it
trades the whitelist's defense-in-depth for a single shell-anything
entry the dashboard owns end-to-end.

The seal closes that hatch. Per ADR CORE-004 the agent **ships
sealed by default** - the seal file is created at ``stormpulse init``
time so a freshly-installed agent advertises the pre-0.1.8 capability
set. The operator runs ``stormpulse signoff unseal`` (with an
interactive hostname-typing confirmation) to open the hatch for
verification, then ``stormpulse signoff seal`` to close it again.

While the seal is OFF the agent nags loudly: periodic warning logs,
dashboard banner via the register payload, and a tracked
``unsealed_since`` timestamp that surfaces in ``stormpulse status``
and the register. The intent is that "agent unsealed for 3 days" be
visible everywhere the operator looks.

State is two files in the agent state directory:

- ``signoff.sealed`` - present iff the agent is sealed.
- ``signoff.unsealed_at`` - present iff the agent is unsealed, contains the
  ISO-8601 UTC timestamp at which it became unsealed. Used for "unsealed
  for X" displays and audit. Absent during the sealed state, and absent
  in the rare hand-edited case where an operator removed the seal file
  directly (we treat that as "unsealed, age unknown" rather than refusing
  to display state).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

_SEAL_FILENAME = "signoff.sealed"
_UNSEALED_AT_FILENAME = "signoff.unsealed_at"


class SignoffState:
    """File-presence seal flag co-located with the agent's nonce DB.

    ``is_sealed()`` re-stats the path on every call so an
    operator-driven seal takes effect for the next inbound command
    without restarting the agent.
    """

    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir)
        self._seal_path = self._dir / _SEAL_FILENAME
        self._unsealed_at_path = self._dir / _UNSEALED_AT_FILENAME

    @property
    def path(self) -> Path:
        """Path to the seal flag file. Kept for back-compat with existing CLI."""
        return self._seal_path

    @property
    def unsealed_at_path(self) -> Path:
        """Path to the unsealed-at-timestamp marker file."""
        return self._unsealed_at_path

    def is_sealed(self) -> bool:
        return self._seal_path.exists()

    def unsealed_since(self) -> datetime | None:
        """Return when the agent became unsealed, or ``None``.

        ``None`` when the agent is sealed, OR when it's unsealed but
        the timestamp marker is missing (e.g. operator removed the seal
        file by hand without going through the CLI). Callers display
        "unsealed for X" only when this returns a value, and fall back
        to "unsealed" otherwise.
        """
        if self.is_sealed():
            return None
        try:
            raw = self._unsealed_at_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    def seal(self) -> bool:
        """Create the seal flag. Returns ``True`` if this call sealed it.

        Idempotent: returns ``False`` if already sealed. Removes the
        unsealed-at marker as part of the same operation so the two
        files never co-exist.
        """
        if self._seal_path.exists():
            # Defensive: clean up a stray marker if it somehow co-exists.
            self._unsealed_at_path.unlink(missing_ok=True)
            return False
        self._dir.mkdir(parents=True, exist_ok=True)
        self._seal_path.touch(mode=0o640)
        self._unsealed_at_path.unlink(missing_ok=True)
        return True

    def unseal(self) -> bool:
        """Remove the seal flag and record when. Returns ``True`` on transition.

        Idempotent: returns ``False`` if already unsealed (the
        timestamp marker is left untouched in that case so the
        original unseal time stays visible).
        """
        if not self._seal_path.exists():
            return False
        # Write the timestamp first so observers that race the seal
        # removal still see *some* coherent state (sealed + about-to-unseal
        # marker, then unsealed + marker). The reverse order would leave
        # a window of "unsealed without a marker" if a crash hit between
        # the two operations.
        self._dir.mkdir(parents=True, exist_ok=True)
        now_iso = datetime.now(UTC).isoformat()
        self._unsealed_at_path.write_text(now_iso, encoding="utf-8")
        self._unsealed_at_path.chmod(0o640)
        self._seal_path.unlink()
        return True


def state_dir_from_db_path(db_path: Path) -> Path:
    """The agent state dir is the directory containing the nonce DB.

    Centralised so config plumbing and the CLI agree on the location.
    """
    return Path(db_path).parent


def format_unsealed_duration(unsealed_since: datetime | None) -> str:
    """Render an unsealed-since timestamp as ``"3h 12m"`` / ``"4d 7h"``.

    Returns ``"unknown"`` when ``unsealed_since`` is None - the caller
    is unsealed but the marker file is missing.
    """
    if unsealed_since is None:
        return "unknown"
    now = datetime.now(UTC)
    delta = now - unsealed_since
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        # Clock skew. Don't crash, just say "moments".
        return "<1m"
    days, remainder = divmod(total_seconds, 86_400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return "<1m"
