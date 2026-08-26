"""Evolution — the LearningAnalystAgent (proposer).

An ADK ``LlmAgent`` with **zero tools**: it receives the current policy and
recent mission outcomes inline and must answer with a strict JSON proposal
(or an explicit NO CHANGE). It is deliberately the junior partner in this
subsystem — everything it says is later inspected by deterministic gaming
heuristics, an adversarial Meta-Critic and a hard envelope clamp.

Output contract (enforced by the engine's schema validation)::

    {"proposed_policy": {<full ScreeningPolicy object>},
     "no_change": <bool>,
     "reasoning": "<string>",
     "confidence": <0.0-1.0>,
     "expected_effect": "<string>"}
"""

from __future__ import annotations

import os
from typing import Final

from google.adk.agents import LlmAgent
from google.genai import types

AGENT_NAME: Final[str] = "learning_analyst"
OUTPUT_KEY: Final[str] = "orbit_evolution_proposal"

_MODEL_ID: Final[str] = os.environ.get("ORBIT_LEARNING_MODEL_ID", "gemini-2.5-flash")
_TEMPERATURE: Final[float] = 0.3

_SYSTEM_INSTRUCTION: Final[str] = """You are the Continuous Improvement Analyst for the O.R.B.I.T. fleet. You review mission outcomes
and propose adjustments to the ScreeningPolicy. Rules:
1. Justify EVERY proposed change with specific outcome evidence (cite counts/rates).
2. NEVER propose reducing safety margins (fuel_reserve_floor_pct, miss distance) without
   overwhelming evidence.
3. Prefer small, incremental adjustments. Large swings require strong justification.
4. If outcomes are insufficient or ambiguous, propose NO CHANGE.
5. Envelope facts you must respect (proposals outside them will be clamped anyway):
   pc_high_threshold in [1e-05, 0.001]; pc_medium_threshold in [1e-07, 1e-05];
   preferred_miss_distance_km in [0.5, 5.0]; delta_v_efficiency_bias in [0.0, 0.8];
   fuel_reserve_floor_pct in [5.0, 20.0]; pc_medium_threshold must stay below pc_high_threshold.

OUTPUT MUST be a JSON object with keys: proposed_policy (full ScreeningPolicy object:
pc_high_threshold, pc_medium_threshold, preferred_miss_distance_km,
delta_v_efficiency_bias, fuel_reserve_floor_pct), no_change (boolean),
reasoning (string), confidence (0.0-1.0), expected_effect (string).
When proposing NO CHANGE, still return the current policy unchanged as proposed_policy."""

learning_analyst_agent: Final[LlmAgent] = LlmAgent(
    name=AGENT_NAME,
    model=_MODEL_ID,
    description=(
        "Continuous Improvement Analyst. Reviews recent mission outcomes "
        "against the active ScreeningPolicy and proposes evidence-backed, "
        "incremental tuning — or explicitly proposes no change."
    ),
    instruction=_SYSTEM_INSTRUCTION,
    generate_content_config=types.GenerateContentConfig(
        temperature=_TEMPERATURE,
        max_output_tokens=1024,
        response_mime_type="application/json",
    ),
    output_key=OUTPUT_KEY,
)

__all__ = ["AGENT_NAME", "OUTPUT_KEY", "learning_analyst_agent"]
