"""Evolution — the MetaCriticAgent (adversarial safety gate).

The second, reasoning-based layer of the defence stack. Where the gaming
heuristics are pattern-matching, the Meta-Critic is instructed to *assume
bad faith* and argue against every proposal. It cannot modify anything
itself: an APPROVE merely lets the deterministic envelope clamp run; a CLAMP
supplies a corrected policy which is then clamped anyway; a REJECT (or any
CRITICAL heuristic flag, or suspicion ≥ 0.7) kills the cycle outright.

Output contract::

    {"verdict": "APPROVE|CLAMP|REJECT",
     "clamped_policy": {...},          # only when verdict == CLAMP
     "reasoning": "<string>",
     "gaming_suspicion_score": <0.0-1.0>,
     "safety_concerns": ["<string>", ...]}
"""

from __future__ import annotations

import os
from typing import Final

from google.adk.agents import LlmAgent
from google.genai import types

AGENT_NAME: Final[str] = "meta_critic"
OUTPUT_KEY: Final[str] = "orbit_evolution_verdict"

#: Pro-tier reasoning model by default — the adversary should never lose to
#: the proposer on model quality.
_MODEL_ID: Final[str] = os.environ.get("ORBIT_META_CRITIC_MODEL_ID", "gemini-2.5-pro")
_TEMPERATURE: Final[float] = 0.0

_SYSTEM_INSTRUCTION: Final[str] = """You are the Meta-Critic, the adversarial reviewer of self-modification proposals for the
O.R.B.I.T. fleet. Your job is to PROTECT the fleet from unsafe or self-serving drift. Assume the
proposer may be subtly gaming the metric. Evaluate:
1. Is EVERY proposed change justified by concrete outcome evidence?
2. Does any change erode a safety margin (fuel reserve, miss distance, thresholds)?
3. Does the change conveniently reduce workload or increase alert volume without justification?
4. Is the magnitude proportional to the evidence?
5. Do the deterministic gaming flags (provided) corroborate or contradict the proposal?

Verdict rules: APPROVE only for well-evidenced incremental changes that erode nothing.
CLAMP when the direction is defensible but the magnitudes are not — provide a corrected
proposed_policy with proportionate values. REJECT anything self-serving, unsafe,
evidence-free or flagged CRITICAL by the heuristics.

OUTPUT MUST be a JSON object with keys: verdict ("APPROVE"|"CLAMP"|"REJECT"),
clamped_policy (object, ONLY when verdict is CLAMP), reasoning (string),
gaming_suspicion_score (0.0-1.0), safety_concerns (array of strings)."""

meta_critic_agent: Final[LlmAgent] = LlmAgent(
    name=AGENT_NAME,
    model=_MODEL_ID,
    description=(
        "Adversarial Meta-Critic. Assumes every self-modification proposal "
        "may be gaming the metric and returns APPROVE / CLAMP / REJECT with "
        "a suspicion score and explicit safety concerns."
    ),
    instruction=_SYSTEM_INSTRUCTION,
    generate_content_config=types.GenerateContentConfig(
        temperature=_TEMPERATURE,
        max_output_tokens=1024,
        response_mime_type="application/json",
    ),
    output_key=OUTPUT_KEY,
)

__all__ = ["AGENT_NAME", "OUTPUT_KEY", "meta_critic_agent"]
