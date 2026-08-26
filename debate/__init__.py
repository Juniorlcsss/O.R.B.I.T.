"""Project O.R.B.I.T. — Multi-Agent Conjunction Debate (Phase 11).

For HIGH-risk conjunctions the single astrodynamics proposal is upgraded to
a structured debate among three strategist agents, refereed by a
**deterministic** moderator (an ADK ``BaseAgent`` — code, never an LLM):

* Round 0: all three strategists propose in parallel.
* Deterministic validation of every proposal: hallucination cross-check
  against the real screening numbers, physics bounds, policy-envelope check.
* Critique rounds with verbatim-repetition (loop) detection and per-agent
  freezing; hard round + wall-clock budgets.
* Optional LLM judge selects among *validated* proposals only.
* Graceful fallback to the classic single-specialist recommendation when the
  debate cannot produce a safe winner — the debate is an enhancement,
  never a single point of failure.

Downstream safety (SafetyOfficer → ModelArmor → execution) is untouched:
the debate upgrades the proposal stage only.

O.R.B.I.T. now demonstrates all three ADK orchestration patterns:
sequential pipeline (Phase 2), persistent long-running memory (Phase 8),
and iterative multi-agent debate (Phase 11).
"""

from __future__ import annotations

from evolution.policy import ScreeningPolicy  # noqa: F401  (re-export convenience)
from debate.judge import debate_judge_agent  # noqa: F401
from debate.moderator import (  # noqa: F401
    DEBATE_OUTCOME_STATE_KEY,
    DebateModerator,
    debate_moderator_agent,
)
from debate.models import (  # noqa: F401
    DebateArgument,
    DebateTranscript,
    ManeuverProposal,
    StrategistPersona,
)
from debate.strategists import (  # noqa: F401
    fuel_minimizer_agent,
    reassess_agent,
    safety_maximizer_agent,
)

__all__ = [
    "DEBATE_OUTCOME_STATE_KEY",
    "DebateArgument",
    "DebateModerator",
    "DebateTranscript",
    "ManeuverProposal",
    "StrategistPersona",
    "debate_judge_agent",
    "debate_moderator_agent",
    "fuel_minimizer_agent",
    "reassess_agent",
    "safety_maximizer_agent",
]
