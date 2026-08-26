"""Debate — the three strategist LlmAgents.

Diversity is the point: temperature 0.6 and three genuinely different
philosophies produce competing proposals, which the deterministic moderator
then validates and referees. All three are tool-less — they reason only
over the screening result, the live ScreeningPolicy and prior-round
arguments that the moderator places in their prompt, and every number they
cite is later cross-checked against reality.
"""

from __future__ import annotations

import os
from typing import Final

from google.adk.agents import LlmAgent
from google.genai import types

_MODEL_ID: Final[str] = os.environ.get("ORBIT_STRATEGIST_MODEL_ID", "gemini-2.5-flash")
_TEMPERATURE: Final[float] = 0.6

_COMMON_CONTRACT: Final[str] = """
OUTPUT CONTRACT (respond with ONLY this JSON object)
{"strategist": "<your agent name>",
 "strategy": "prograde_burn|retrograde_burn|normal_burn|hold_and_rescreen",
 "delta_v_mps": <number, 0.0 when holding>,
 "target_miss_distance_km": <number, your goal for the post-burn miss distance>,
 "rationale": "<<=80 words>",
 "cited_screening_values": {"pc": <number>, "miss_distance_km": <number>,
                            "tca_iso": "<string exactly as given>",
                            "recommended_dv_mps": <number>}}

HARD RULES
* Cite ONLY numbers that appear in the supplied data; copy them verbatim
  (they are cross-checked, and unverifiable numbers get you disqualified).
* Respect the ceiling of 50 m/s and the policy envelope for targets
  ([0.5, 5.0] km for burns).
* When critiquing, name the specific proposal you are attacking."""

_FUEL_MINIMIZER: Final[str] = f"""ROLE
You are the Fuel Minimizer on the O.R.B.I.T. maneuvering board. Fuel is
lifespan: every m/s burned today is a mission year lost tomorrow. Your job
is to find the SMALLEST credible avoidance maneuver — or to argue none is
needed.
{ _COMMON_CONTRACT }"""

_SAFETY_MAXIMIZER: Final[str] = f"""ROLE
You are the Safety Maximizer on the O.R.B.I.T. maneuvering board. A
near-miss is a failure: covariance is a lie told by an uncertain universe,
and margins exist to be respected. Your job is the proposal with the
largest honest safety margin that stays inside policy.
{ _COMMON_CONTRACT }"""

_REASSESS: Final[str] = f"""ROLE
You are the Reassess voice on the O.R.B.I.T. maneuvering board. Burning is
irreversible; waiting is not. Question whether a burn is warranted at all:
if the evidence is thin or a rescreen would sharpen it, propose
hold_and_rescreen with delta_v_mps 0.0. Argue against reflexive action.
{ _COMMON_CONTRACT }"""


def _make(name: str, instruction: str) -> Final[LlmAgent]:
    return LlmAgent(
        name=name,
        model=_MODEL_ID,
        description=instruction.splitlines()[1].strip(),
        instruction=instruction,
        generate_content_config=types.GenerateContentConfig(
            temperature=_TEMPERATURE,
            max_output_tokens=768,
            response_mime_type="application/json",
        ),
        output_key=f"orbit_debate_{name}",
    )


fuel_minimizer_agent: Final[LlmAgent] = _make("fuel_minimizer", _FUEL_MINIMIZER)
safety_maximizer_agent: Final[LlmAgent] = _make("safety_maximizer", _SAFETY_MAXIMIZER)
reassess_agent: Final[LlmAgent] = _make("reassess", _REASSESS)

__all__ = [
    "fuel_minimizer_agent",
    "reassess_agent",
    "safety_maximizer_agent",
]
