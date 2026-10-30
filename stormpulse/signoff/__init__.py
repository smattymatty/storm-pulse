"""Seal state for dashboard-dispatched, HMAC-signed shell commands.

CORE-004 d6: the agent is open only while signoff.unsealed_at exists, and only
`signoff unseal` writes it, so a missing, wiped or migrated state dir is sealed.
signoff.sealed is still written so an agent older than 0.8.3 reads it sealed."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

_SEAL_FILENAME = "signoff.sealed"
_UNSEALED_AT_FILENAME = "signoff.unsealed_at"


class SignoffState:
    """Store the seal beside the nonce DB.

    Recheck the file on each command so seal changes need no agent restart."""

    def __init__(self, state_dir: Path) -> None:
        self._dir = Path(state_dir)
        self._seal_path = self._dir / _SEAL_FILENAME
        self._unsealed_at_path = self._dir / _UNSEALED_AT_FILENAME

    @property
    def path(self) -> Path:
        """Return the seal path, retained for CLI compatibility."""
        return self._seal_path

    @property
    def unsealed_at_path(self) -> Path:
        """Return the unseal timestamp path."""
        return self._unsealed_at_path

    def is_sealed(self) -> bool:
        return not self._unsealed_at_path.is_file()

    def unsealed_since(self) -> datetime | None:
        """Return the unseal timestamp, or None if sealed, missing, or invalid.

        Callers show elapsed time when known, otherwise just "unsealed"."""
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
        """Remove the unseal marker; return True on transition.

        The marker goes first, so a crash before the legacy file lands is sealed."""
        was_open = not self.is_sealed()
        self._unsealed_at_path.unlink(missing_ok=True)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._seal_path.touch(mode=0o640)
        return was_open

    def unseal(self) -> bool:
        """Write the unseal marker with the time; return True on transition.

        Already unsealed returns False and preserves the original timestamp."""
        if not self.is_sealed():
            return False
        self._dir.mkdir(parents=True, exist_ok=True)
        now_iso = datetime.now(UTC).isoformat()
        self._unsealed_at_path.write_text(now_iso, encoding="utf-8")
        self._unsealed_at_path.chmod(0o640)
        self._seal_path.unlink(missing_ok=True)
        return True


def state_dir_from_db_path(db_path: Path) -> Path:
    """Use the nonce DB's parent as the shared agent state directory."""
    return Path(db_path).parent


def format_unsealed_duration(unsealed_since: datetime | None) -> str:
    """Format elapsed time as "3h 12m" or "4d 7h"; None yields "unknown"."""
    if unsealed_since is None:
        return "unknown"
    now = datetime.now(UTC)
    delta = now - unsealed_since
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        # Treat future timestamps from clock skew as less than a minute ago.
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
