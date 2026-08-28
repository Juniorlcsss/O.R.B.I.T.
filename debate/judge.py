"""Debate — the DebateJudge LlmAgent (selection among validated proposals only).

Runs ONLY when multiple valid proposals survive all rounds without numeric
convergence. It is deliberately powerless: it may not invent a maneuver,
change numbers, or propose anything new — the moderator enforces that its
``winner`` field names one of the validated proposals, or the debate falls
back to the classic single-specialist recommendation.
"""

from __future__ import annotations

import os
from typing import Final

from google.adk.agents import LlmAgent
from google.genai import types
from geap_sim.model_config import structured_json_config

AGENT_NAME: Final[str] = "debate_judge"
OUTPUT_KEY: Final[str] = "orbit_debate_judge"

_MODEL_ID: Final[str] = os.environ.get("ORBIT_DEBATE_JUDGE_MODEL_ID", "gemini-3.7-flash")
_TEMPERATURE: Final[float] = 0.0

_SYSTEM_INSTRUCTION: Final[str] = """Pick the single best maneuver by weighing safety margin, fuel efficiency, and
operational feasibility, consistent with the current ScreeningPolicy. You may only choose
among the validated proposals. Justify your choice and name the trade-offs you rejected.

OUTPUT MUST be a JSON object with keys: winner (the exact "strategist" name of ONE of the
listed proposals), justification (string), tradeoffs_rejected (array of strings).
You MUST NOT invent proposals, numbers, or strategist names."""

debate_judge_agent: Final[LlmAgent] = LlmAgent(
    name=AGENT_NAME,
    model=_MODEL_ID,
    description=(
        "Debate adjudicator. Selects the winning maneuver strictly among "
        "the moderator-validated proposals; cannot introduce new options."
    ),
    instruction=_SYSTEM_INSTRUCTION,
    generate_content_config=structured_json_config(
        answer_tokens=512,
        temperature=_TEMPERATURE,
    ),
    output_key=OUTPUT_KEY,
)

__all__ = ["AGENT_NAME", "OUTPUT_KEY", "debate_judge_agent"]
