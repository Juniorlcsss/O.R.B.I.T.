"""O.R.B.I.T. multi-agent fleet (Google ADK).

Modules are intentionally separated to enforce single-responsibility agents:

* ``admiral``      — FleetAdmiralAgent: constellation-level fuel triage for
  bursts of simultaneous conjunctions. Root of the agent tree; a pure
  pass-through for single alerts.
* ``orchestrator`` — FleetCommanderAgent: deterministic mission pipeline,
  circuit breakers, routing, edge-autonomy fallback. Never calls tools
  directly.
* ``astro``        — AstrodynamicsAgent: orbital math + TLE tools ONLY.
* ``diplomat``     — DiplomatAgent: external fleet negotiation tools ONLY.
* ``safety``       — SafetyOfficerAgent: guardrail with NO tools. The sole
  authority for approving or rejecting manoeuvres on the ground.
* ``edge_agent``   — Gemma Edge Autopilot: satellite-side loss-of-signal
  autonomy with EXACTLY ONE tool (emergency_dodge).
* ``watcher``      — WatchCommander: persistent multi-day conjunction watches.

The self-evolution subsystem (Phase 10) lives in the sibling ``evolution``
package; its agents are re-exported here for registry/tree visibility.
"""

from __future__ import annotations

from typing import Final

from .astro import astrodynamics_agent
from .diplomat import diplomat_agent
from .edge_agent import gemma_edge_agent
from .orchestrator import fleet_commander_agent
from .admiral import fleet_admiral_agent
from .safety import safety_officer_agent
from .watcher import watcher_agent

# Imported AFTER orchestrator so the package's own imports have settled.
from debate.judge import debate_judge_agent
from debate.strategists import fuel_minimizer_agent, reassess_agent, safety_maximizer_agent
from evolution.engine import EvolutionEngine, EvolutionReport
from evolution.learning_analyst import learning_analyst_agent
from evolution.meta_critic import meta_critic_agent

from .orchestrator import fleet_commander_agent as _fleet  # noqa: F401  (ensures moderator wired)

__version__: Final[str] = "0.12.0"

__all__ = [
    "__version__",
    "EvolutionEngine",
    "EvolutionReport",
    "astrodynamics_agent",
    "debate_judge_agent",
    "diplomat_agent",
    "fleet_admiral_agent",
    "fleet_commander_agent",
    "fuel_minimizer_agent",
    "gemma_edge_agent",
    "learning_analyst_agent",
    "meta_critic_agent",
    "reassess_agent",
    "safety_maximizer_agent",
    "safety_officer_agent",
    "watcher_agent",
]
