"""The capability-provider registry (P2, CORE-007).

Framework layer. A provider registers under a capability token; the engine looks
it up by token and never imports the Feature that owns the capability (I13). In
P2 no real provider is registered here (the ``caddy.drop_in.v1`` provider that
mutates an operator's Caddy config lands with buckets-gate, P5); the registry and
its dispatch are proven with a synthetic provider.
"""

from __future__ import annotations

from stormpulse.sdk import is_capability_token
from stormpulse.wizard.env import CapabilityProvider

_providers: dict[str, CapabilityProvider] = {}


def register_capability_provider(token: str, provider: CapabilityProvider) -> None:
    """Register a provider for a capability token. A duplicate token is refused
    (one provider per token, the engine-side mirror of I13)."""
    if not is_capability_token(token):
        raise ValueError(f"not a capability token: {token!r}")
    if token in _providers:
        raise ValueError(f"capability {token!r} already has a provider")
    _providers[token] = provider


def get_provider(token: str) -> CapabilityProvider | None:
    """The registered provider for a token, or ``None``."""
    return _providers.get(token)


def registered_providers() -> dict[str, CapabilityProvider]:
    """A copy of the registered provider map (e.g. to build an ``ApplyEnv``)."""
    return dict(_providers)


def _reset_for_tests() -> None:
    """Clear the registry. Test-only; the production path registers at import."""
    _providers.clear()
