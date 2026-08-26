"""O.R.B.I.T. multi-agent fleet (Google ADK).

Modules are intentionally separated to enforce single-responsibility agents:

* ``orchestrator`` â€” FleetCommanderAgent: deterministic mission pipeline,
  circuit breakers, routing, edge-autonomy fallback. Never calls tools
  directly.
* ``astro``        â€” AstrodynamicsAgent: orbital math + TLE tools ONLY.
* ``diplomat``     â€” DiplomatAgent: external fleet negotiation tools ONLY.
* ``safety``       â€” SafetyOfficerAgent: guardrail with NO tools. The sole
  authority for approving or rejecting manoeuvres on the ground.
* ``edge_agent``   â€” Gemma Edge Autopilot: satellite-side loss-of-signal
  autonomy with EXACTLY ONE tool (emergency_dodge).
"""

from __future__ import annotations

from typing import Final

from .astro import astrodynamics_agent
from .diplomat import diplomat_agent
from .edge_agent import gemma_edge_agent
from .orchestrator import fleet_commander_agent
from .safety import safety_officer_agent

__version__: Final[str] = "0.6.0"

__all__ = [
    "__version__",
    "astrodynamics_agent",
    "diplomat_agent",
    "fleet_commander_agent",
    "gemma_edge_agent",
    "safety_officer_agent",
]
