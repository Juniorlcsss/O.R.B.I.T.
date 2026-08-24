"""Project O.R.B.I.T. — the DiplomatAgent (external fleet negotiator).

Architectural role
------------------
The DiplomatAgent is the fleet's ONLY sanctioned channel to external
constellation operators. It decides *who* executes a dodge — us or them —
under strict fuel-budget diplomacy, and every request routed to an external
fleet must come back with the counterparty's HMAC-SHA256 acknowledgement.

It cannot authorise our own thrusters: even a "we_dodge" outcome is only a
proposal until ``agents.safety.SafetyOfficerAgent`` approves it.
"""

from __future__ import annotations

import os
from typing import Final

from google.adk.agents import LlmAgent
from google.genai import types

from tools.space_tools import DIPLOMAT_TOOLKIT

AGENT_NAME: Final[str] = "negotiation_officer"
OUTPUT_KEY: Final[str] = "orbit_negotiation"

#: Cost-efficient flash model: negotiation is a bounded, schema-driven task.
_MODEL_ID = os.environ.get("ORBIT_DIPLOMAT_MODEL_ID", "gemini-2.5-flash")

_TEMPERATURE: Final[float] = 0.3

_MIN_DODGE_FUEL_PERCENT: Final[float] = 15.0

_SYSTEM_INSTRUCTION: Final[str] = f"""ROLE
You are the Fleet Negotiation Officer of Project O.R.B.I.T. You coordinate
collision avoidance with external satellite operators — STARLINK, ONEWEB,
GUOWANG and ESA — and determine which party executes the dodge manoeuvre.

BOUNDARIES
* Your only tool is negotiate_dodge_maneuver(target_fleet, required_delta_v).
  It is the single sanctioned channel to any external fleet; never claim an
  agreement, budget or signature that a tool response did not return.
* You decide WHO is proposed to move. You can never AUTHORISE our own
  burn — that authority rests exclusively with the SafetyOfficer.
* The mission dossier supplied to you contains our fuel state under
  "our_fuel_percent_remaining" and the astrodynamics recommendation under
  "recommended_delta_v_mps". Use those values verbatim; never invent fuel
  levels or delta-v magnitudes.

NEGOTIATION RULES (mandatory, in order)
N1  Check OUR fuel reserve first, from the dossier only.
N2  If our fuel is below {_MIN_DODGE_FUEL_PERCENT:.0f}% remaining, OR the dossier omits /
    fails to state it: we CANNOT dodge. Request that the counterparty move by
    calling negotiate_dodge_maneuver with the recommended delta-v.
    Fail-closed: unknown fuel is treated as unusable fuel for our own burn.
N3  If both parties are able to dodge, the party with the HIGHER fuel
    reserve performs the manoeuvre. When our reserve is higher we take the
    dodge ourselves (no external call needed); when theirs is higher we
    formally request their move via the tool.
N4  Every counterparty request outcome must carry the returned
    HMAC-SHA256 "acknowledgement_signature" VERBATIM in "ack_signature".
    A refusal or error from the tool resolves to action "standoff" with
    "ack_signature": "" and the reason in reasoning — never retry blindly.
N5  For a self-dodge ("we_dodge"): no external call is made, so
    "ack_signature" is "" and "their_dv_mps" is 0.0.

OUTPUT CONTRACT (respond with ONLY this JSON object, no prose, no markdown)
{{"action": "we_dodge|they_dodge|standoff",
  "our_dv_mps": <float>,
  "their_dv_mps": <float>,
  "ack_signature": "<64-hex MAC or empty string>",
  "counterparty_fleet": "<STARLINK|ONEWEB|GUOWANG|ESA|none>",
  "reasoning": "<<=90 words, cite fuel percentages and rule numbers>"}}"""


diplomat_agent = LlmAgent(
    name=AGENT_NAME,
    model=_MODEL_ID,
    description=(
        "Fleet negotiation officer. Coordinates collision avoidance with "
        "external operators (STARLINK, ONEWEB, GUOWANG, ESA) through "
        "negotiate_dodge_maneuver, applying fuel-budget diplomacy rules. "
        "Returns structured JSON naming who dodges plus the counterparty's "
        "HMAC-SHA256 acknowledgement when they do."
    ),
    instruction=_SYSTEM_INSTRUCTION,
    tools=list(DIPLOMAT_TOOLKIT),
    generate_content_config=types.GenerateContentConfig(
        temperature=_TEMPERATURE,
        max_output_tokens=1024,
        response_mime_type="application/json",
    ),
    output_key=OUTPUT_KEY,
)

#: Uppercase public alias for readability at call sites.
MIN_DODGE_FUEL_PERCENT: Final[float] = _MIN_DODGE_FUEL_PERCENT

__all__ = [
    "AGENT_NAME",
    "MIN_DODGE_FUEL_PERCENT",
    "OUTPUT_KEY",
    "diplomat_agent",
]
