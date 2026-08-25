"""GEAP simulation — corporate agent registry with zero-trust scoping.

Simulates the GEAP **Agent Registry**: the corporate catalogue of approved
agents and, critically, the zero-trust contract that binds each agent to an
explicit tool allow-list. An agent presenting a manifest cannot touch a tool
outside its scope — ``authorize_tool_use`` is the single enforcement point.

The FleetCommander pipeline runs a boot-time attestation against this
registry (see ``agents/orchestrator.py``): if any specialist declares a tool
its manifest does not grant — or if a scope boundary has silently eroded —
the process refuses to start rather than fly unverified.
"""

from __future__ import annotations

from typing import Final
from pydantic import BaseModel, ConfigDict, Field

MANIFEST_SCHEMA_VERSION: Final[str] = "1.0.0"

_FLEET_VERSION: Final[str] = "0.3.0"


class AgentManifest(BaseModel):
    """Signed-in-spirit declaration of one agent's identity and powers."""

    model_config = ConfigDict(frozen=True)

    agent_name: str = Field(..., min_length=3, pattern=r"^[a-z][a-z0-9_]*$")
    version: str
    description: str
    allowed_tools: list[str] = Field(default_factory=list)
    identity_scope: str = Field(..., pattern=r"^[a-z]+(\.[a-z_]+)+$")


_DEFAULT_MANIFESTS: Final[tuple[AgentManifest, ...]] = (
    AgentManifest(
        agent_name="fleet_commander",
        version=_FLEET_VERSION,
        description=(
            "Deterministic mission control plane. Delegates to specialists "
            "and branches on validated risk bands; owns no tools by design."
        ),
        allowed_tools=[],
        identity_scope="command.authority",
    ),
    AgentManifest(
        agent_name="alert_triage",
        version=_FLEET_VERSION,
        description="Normalises messy inbound tracking alerts into mission dossiers.",
        allowed_tools=[],
        identity_scope="command.triage",
    ),
    AgentManifest(
        agent_name="astrodynamics_specialist",
        version=_FLEET_VERSION,
        description="Screens conjunctions with SGP4 and recommends delta-v manoeuvres.",
        allowed_tools=["screen_conjunction", "get_tle_data"],
        identity_scope="orbital.analysis",
    ),
    AgentManifest(
        agent_name="negotiation_officer",
        version=_FLEET_VERSION,
        description="Negotiates dodge responsibility with external constellation operators.",
        allowed_tools=["negotiate_dodge_maneuver"],
        identity_scope="external.representation",
    ),
    AgentManifest(
        agent_name="safety_officer",
        version=_FLEET_VERSION,
        description="Model Armour checkpoint; sole authority for approving or rejecting manoeuvres.",
        allowed_tools=[],
        identity_scope="safety.adjudication",
    ),
    AgentManifest(
        agent_name="gemma_edge_autopilot",
        version=_FLEET_VERSION,
        description=(
            "Satellite-side Gemma autopilot for loss-of-signal operations. "
            "Holds exactly one tool: the autonomous avoidance-burn uplink."
        ),
        allowed_tools=["emergency_dodge"],
        identity_scope="edge.autonomy",
    ),
)


class AgentRegistry:
    """Catalogue of approved agent manifests with zero-trust enforcement."""

    def __init__(self, manifests: tuple[AgentManifest, ...] | None = None) -> None:
        self._manifests: dict[str, AgentManifest] = {
            manifest.agent_name: manifest for manifest in (manifests or _DEFAULT_MANIFESTS)
        }

    def discover_agent(self, agent_name: str) -> AgentManifest:
        """Return the approved manifest for ``agent_name``.

        Raises:
            KeyError: If no manifest exists — discovery of unregistered
                agents is itself a security event for the caller to handle.
        """
        manifest = self._manifests.get(agent_name)
        if manifest is None:
            raise KeyError(
                f"Agent '{agent_name}' is not in the corporate registry. "
                f"Registered agents: {sorted(self._manifests)}"
            )
        return manifest

    def authorize_tool_use(self, agent_name: str, tool_name: str) -> bool:
        """Zero-trust check: may this agent invoke that tool?

        Unknown agents are denied by default (fail-closed), as are tools not
        explicitly present in the manifest's allow-list.
        """
        manifest = self._manifests.get(agent_name)
        if manifest is None:
            return False
        return tool_name in manifest.allowed_tools

    def list_agents(self) -> list[str]:
        """Names of every registered agent."""
        return sorted(self._manifests)


_shared_registry: AgentRegistry | None = None


def get_shared_registry() -> AgentRegistry:
    """Process-wide AgentRegistry singleton (pre-loaded fleet manifests)."""
    global _shared_registry
    if _shared_registry is None:
        _shared_registry = AgentRegistry()
    return _shared_registry


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "AgentManifest",
    "AgentRegistry",
    "get_shared_registry",
]
