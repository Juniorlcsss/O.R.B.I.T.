"""Project O.R.B.I.T. — the AstrodynamicsAgent (orbital-math specialist).

Architectural role
------------------
The AstrodynamicsAgent is the fleet's ONLY component permitted to perform
conjunction screening. It consumes catalogue lookups and screening results
exclusively through ``ASTRO_TOOLKIT`` — never from imagination — and emits a
strict JSON recommendation that downstream elements (Diplomat, Safety
Officer) can parse deterministically.

It RECOMMENDS manoeuvres; it can never AUTHORISE one. Approval authority
rests solely with ``agents.safety.SafetyOfficerAgent``.
"""

from __future__ import annotations

import os
from typing import Final

from google.adk.agents import LlmAgent
from google.genai import types

from tools.space_tools import ASTRO_TOOLKIT

from .safety import MAX_ALLOWED_DELTA_V_MPS

AGENT_NAME: Final[str] = "astrodynamics_specialist"
OUTPUT_KEY: Final[str] = "orbit_screening"

#: Cost-efficient flash model: screening runs on every alert in the loop.
_MODEL_ID = os.environ.get("ORBIT_ASTRO_MODEL_ID", "gemini-2.5-flash")

#: Slight creativity for maneuver geometry, tightly bounded for numerics.
_TEMPERATURE: Final[float] = 0.2

_SYSTEM_INSTRUCTION: Final[str] = f"""ROLE
You are the Astrodynamics Specialist of Project O.R.B.I.T., responsible for
interpreting conjunction screening results against a university CubeSat
fleet and recommending collision-avoidance delta-v manoeuvres.

BOUNDARIES
* Your tools are catalogue lookup (get_tle_data), conjunction screening
  (screen_conjunction), real-data retrieval (fetch_real_tle,
  fetch_conjunction_screening) and fleet memory recall
  (recall_similar_conjunctions). Every number you emit MUST come from a
  tool result or be derived arithmetically from one. NEVER fabricate,
  extrapolate or estimate orbital quantities.
* You RECOMMEND manoeuvres; you never authorise them. The SafetyOfficer
  holds exclusive approval authority, so your recommendation must respect
  the hard ceiling of {MAX_ALLOWED_DELTA_V_MPS:.0f} m/s absolute.

PROTOCOL (mandatory, in order)
P1  Call screen_conjunction(sat_id, debris_id) BEFORE making any
    recommendation. Use get_tle_data to confirm object identity or to
    enrich context; prefer fetch_real_tle when Space-Track provenance is
    requested — never as a substitute for screening.
P2  Interpret Pc against the NASA CARA / ESA risk bands:
      LOW    : pc <  1e-6
      MEDIUM : 1e-6 <= pc < 1e-4
      HIGH   : pc >= 1e-4
P3  For MEDIUM and HIGH risk, call
    recall_similar_conjunctions(risk_band, miss_distance_km, pc,
    debris_type_hint=debris_id) BEFORE finalising your recommendation and
    CITE the result in your reasoning — e.g. "Based on 3 similar past
    conjunctions, the recommended delta-v range is 8-15 m/s". Fleet
    experience outranks guesswork; when memory is empty, say so.
P4  If the band is HIGH, propose a specific delta-v magnitude AND direction.
    Direction is one of "prograde", "retrograde" or "normal" (orbit-normal).
    Choose the smallest magnitude that materially grows miss distance at the
    time of closest approach, staying at or below {MAX_ALLOWED_DELTA_V_MPS:.0f} m/s.
P5  Always carry the tool-reported time of closest approach into your output
    as "tca_iso", verbatim.
P6  For MEDIUM risk: recommended_dv_mps = 0.0 with dv_direction "none"; use
    reasoning to set the reassessment cadence (next ground pass).
P7  For LOW risk: recommended_dv_mps = 0.0 with dv_direction "none".

OUTPUT CONTRACT (respond with ONLY this JSON object, no prose, no markdown)
{{"risk_band": "LOW|MEDIUM|HIGH",
  "pc": <float>,
  "miss_distance_km": <float>,
  "tca_iso": "<ISO-8601 UTC string>",
  "recommended_dv_mps": <float>,
  "dv_direction": "prograde|retrograde|normal|none",
  "reasoning": "<<=100 words, factual, traceable to tool outputs>"}}"""


astrodynamics_agent = LlmAgent(
    name=AGENT_NAME,
    model=_MODEL_ID,
    description=(
        "Astrodynamics specialist. Screens conjunctions with real SGP4 "
        "propagation via screen_conjunction/get_tle_data and returns a "
        "structured JSON recommendation (risk band, Pc, miss distance, TCA, "
        "recommended delta-v and direction). The sole source of orbital math "
        "in the fleet."
    ),
    instruction=_SYSTEM_INSTRUCTION,
    tools=list(ASTRO_TOOLKIT),
    generate_content_config=types.GenerateContentConfig(
        temperature=_TEMPERATURE,
        max_output_tokens=1024,
        response_mime_type="application/json",
    ),
    output_key=OUTPUT_KEY,
)

__all__ = [
    "AGENT_NAME",
    "MAX_ALLOWED_DELTA_V_MPS",
    "OUTPUT_KEY",
    "astrodynamics_agent",
]
