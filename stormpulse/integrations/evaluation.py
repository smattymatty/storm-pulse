"""Contain failures at integration callbacks, without hiding kernel failures."""

from collections.abc import Callable
from typing import Any

from stormpulse.config import ConfigError
from stormpulse.integrations.registry import Integration


class IntegrationCallbackError(Exception):
    """A safe, actionable diagnostic for a failed integration callback."""

    def __init__(self, integration_id: str, stage: str, error_type: str) -> None:
        self.stage = stage
        super().__init__(
            f"Integration '{integration_id}' couldn't complete its {stage} check "
            f"({error_type}). Check its configuration and host access, then run "
            f"'stormpulse integration doctor {integration_id}'. "
            "If the check still fails, update the integration and restart Storm Pulse."
        )


def evaluate_callback[T](
    integration_id: str, stage: str, callback: Callable[[], T]
) -> T:
    """Catch ordinary callback failures; deliberate safe config errors propagate."""
    try:
        return callback()
    except ConfigError:
        raise
    except Exception as exc:
        raise IntegrationCallbackError(
            integration_id, stage, type(exc).__name__
        ) from None


def parse_integration_config(integ: Integration, raw: dict[str, object]) -> Any:
    return evaluate_callback(integ.id, "configuration", lambda: integ.parse_config(raw))


def integration_enabled(integ: Integration, parsed: Any) -> bool:
    result = evaluate_callback(integ.id, "enablement", lambda: integ.enabled(parsed))
    if type(result) is not bool:
        raise IntegrationCallbackError(integ.id, "enablement", "expected true or false")
    return result


def integration_preconditions(integ: Integration, parsed: Any) -> str | None:
    callback = integ.preconditions
    if callback is None:
        return None
    result = evaluate_callback(integ.id, "host requirements", lambda: callback(parsed))
    if result is not None and (not isinstance(result, str) or not result.strip()):
        raise IntegrationCallbackError(
            integ.id, "host requirements", "expected a reason or None"
        )
    return result
