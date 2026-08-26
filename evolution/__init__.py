"""Project O.R.B.I.T. — the self-evolution subsystem (Phase 10).

The fleet reviews its own mission outcomes and tunes its ScreeningPolicy —
under three independent layers of protection:

1. **Deterministic hard envelope** (``policy.clamp_to_envelope``) — pure
   code that bounds, step-limits and invariant-checks every proposal. It
   runs ALWAYS, even on APPROVED changes; no LLM output is ever saved raw.
2. **Adversarial Meta-Critic** (``meta_critic``) — a separate LLM whose
   only job is to assume the proposer is gaming the metric.
3. **Freeze circuit-breaker** (``engine``) — repeated rejections or
   repeated envelope-pushing halts evolution entirely until a human
   unfreezes it.

The linchpin that makes evolution real: ``tools.space_tools.screen_conjunction``
reads the *live* policy for its risk bands, so an applied cycle changes the
very next screening decision.
"""

from __future__ import annotations

from evolution.engine import EvolutionEngine, EvolutionReport  # noqa: F401
from evolution.gaming import GamingFlag, detect_gaming  # noqa: F401
from evolution.learning_analyst import learning_analyst_agent  # noqa: F401
from evolution.meta_critic import meta_critic_agent  # noqa: F401
from evolution.outcome import MissionOutcome, OutcomeSimulator  # noqa: F401
from evolution.policy import (  # noqa: F401
    EVOLUTION_ENVELOPE,
    MAX_STEP_FRACTION,
    PolicyStore,
    ScreeningPolicy,
    clamp_to_envelope,
    validate_invariants,
)

__all__ = [
    "EVOLUTION_ENVELOPE",
    "MAX_STEP_FRACTION",
    "EvolutionEngine",
    "EvolutionReport",
    "GamingFlag",
    "MissionOutcome",
    "OutcomeSimulator",
    "PolicyStore",
    "ScreeningPolicy",
    "clamp_to_envelope",
    "detect_gaming",
    "learning_analyst_agent",
    "meta_critic_agent",
    "validate_invariants",
]
