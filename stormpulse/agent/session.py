"""Session compatibility is separate from integration grants and node config."""

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SessionCompatibility:
    agent_digest: str
    control_plane_digest: str

    @property
    def compatible(self) -> bool:
        return (
            isinstance(self.agent_digest, str)
            and bool(re.fullmatch(r"sha256:[0-9a-f]{64}", self.agent_digest))
            and self.agent_digest == self.control_plane_digest
        )

    @property
    def guidance(self) -> str:
        return (
            "Commands are paused because this agent and the control plane use different "
            "protocol contracts. Metrics, logs and events can still be sent. "
            "Update the control plane's Storm Pulse contract and its parser together, "
            "then reconnect the agent. To restore service sooner, roll back the incompatible release. "
            f"Agent: {str(self.agent_digest)[:95]!r}; control plane: {str(self.control_plane_digest)[:95]!r}."
        )
