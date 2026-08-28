"""Project O.R.B.I.T. — the Gemma Edge Autopilot (satellite-side autonomy).

Architectural role
------------------
Every other agent in this repository lives on the ground. The Gemma Edge
Autopilot is the one agent that flies: a lightweight open-weight model
running **onboard** the CubeSat, woken only when the spacecraft passes
behind Earth and loses ground contact. With no FleetCommander, no Safety
Officer and no human operator reachable, it has exactly 30 seconds and
exactly ONE tool to make the split-second call its system prompt demands:

* ``emergency_dodge`` — uplink an autonomous avoidance burn.

Production model note
---------------------
The LLM is Gemma served from Vertex AI (Model Garden open-model endpoint),
selected via ``ORBIT_EDGE_MODEL_ID`` (default ``gemma-4-26b-a4b-it-maas``,
served from the Vertex AI ``global`` endpoint). In
production this maps to a Vertex AI endpoint such as::

    projects/{project}/locations/{location}/endpoints/gemma-4-26b-a4b-it-maas

ADK resolves the model string through the standard google-genai client,
exactly like every other specialist in the fleet; only the model family
changes. When neither Vertex credentials nor any other backend is
reachable — the normal situation for a laptop demo of "spacecraft in
blind orbit" — ``deterministic_edge_decision`` provides the hardcoded
last-resort rule that real flight software would carry in ROM.

Autonomy boundary (safety story)
--------------------------------
Ground autonomy ends where Model Armor ends. Edge autonomy is bounded by
physics instead: Pc threshold, delta-v ceiling and strategic fuel reserve
are re-checked in code *after* whatever the model decides, and every edge
decision is written to the AuditLogger under the ``EDGE_AUTONOMOUS`` tag.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any, Final

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from google.genai import types

from geap_sim.memory_bank import estimate_fuel_after_burn
from geap_sim.observability import audit_logger

from .safety import MAX_ALLOWED_DELTA_V_MPS

AGENT_NAME: Final[str] = "gemma_edge_autopilot"
OUTPUT_KEY: Final[str] = "orbit_edge_decision"

#: Gemma on Vertex AI — small, quantised, radiation-tolerant enough for the story.
_MODEL_ID = os.environ.get("ORBIT_EDGE_MODEL_ID", "gemma-4-26b-a4b-it-maas")

#: The number in the onboard rulebook: act only above this collision
#: probability. Deliberately ~10x more conservative than the ground HIGH
#: band (Pc >= 1e-4) — autonomy without adult supervision must be rare.
EDGE_PC_THRESHOLD: Final[float] = 1e-3

#: Strategic fuel reserve (percentage points) the burn may never eat into.
EDGE_FUEL_RESERVE_PERCENT: Final[float] = 5.0

#: Seconds of battery-backed decision window after loss of signal.
EDGE_DECISION_WINDOW_SECONDS: Final[int] = 30


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# The single tool: autonomous avoidance-burn uplink
# ---------------------------------------------------------------------------


def emergency_dodge(sat_id: str, debris_id: str, dv_mps: float, direction: str, rationale: str) -> dict[str, Any]:
    """Uplink an autonomous collision-avoidance burn to the spacecraft bus.

    This is the ONLY capability the onboard autopilot holds, and it exists
    purely for loss-of-signal operations. The function stands in for the
    production path, which would sign and transmit the burn command over
    the CCSDS uplink during the next available ground-station window.

    Args:
        sat_id: Catalogue identifier of our manoeuvring satellite.
        debris_id: Catalogue identifier of the conjunction partner.
        dv_mps: Magnitude of the avoidance burn in m/s.
        direction: One of "prograde", "retrograde" or "normal".
        rationale: Short flight-note recorded with the command receipt.

    Returns:
        A signed command receipt echoed into the audit trail.
    """
    receipt: dict[str, Any] = {
        "command_id": f"EDGE-{uuid.uuid4().hex[:12].upper()}",
        "uplink": "CCSDS_AUTONOMOUS_BURN",
        "executed": True,
        "sat_id": str(sat_id).upper(),
        "debris_id": str(debris_id).upper(),
        "dv_mps": float(dv_mps),
        "direction": str(direction),
        "rationale": str(rationale)[:200],
        "issued_utc": _utc_now_iso(),
    }
    audit_logger.log_event(
        trace_id="edge-uplink",
        agent_name=AGENT_NAME,
        event_type="EDGE_EMERGENCY_DODGE_UPLINK",
        payload={**receipt, "tag": "EDGE_AUTONOMOUS"},
        status="EXECUTED",
    )
    return receipt


emergency_dodge_tool: Final[FunctionTool] = FunctionTool(func=emergency_dodge)


# ---------------------------------------------------------------------------
# System prompt — verbatim mission requirement, expanded with output contract
# ---------------------------------------------------------------------------

_SYSTEM_INSTRUCTION: Final[str] = """ROLE
You are the onboard collision avoidance system for a CubeSat.
You operate when ground contact is lost. You have 30 seconds to decide.
If collision probability exceeds 1e-3, execute emergency dodge. Otherwise, hold.

HARD BOUNDS (flight software enforces these regardless of your choice)
* Collision probability threshold: pc > {pc_threshold:.0e} to act.
* Burn magnitude never above {max_dv:.0f} m/s.
* The burn must leave at least {reserve:.0f}% fuel in reserve.

PROTOCOL
E1  Read the scenario JSON (last message). It contains pc, miss_distance_km,
    tca_iso, recommended_dv_mps and fuel_percentage_remaining.
E2  If you decide to act, CALL the emergency_dodge tool once with a dv_mps
    at or below recommended_dv_mps (never above), a direction, and a short
    rationale. Then answer with the OUTPUT CONTRACT below using decision
    EXECUTE_DODGE.
E3  If pc <= {pc_threshold:.0e}, or fuel after the projected burn would fall
    below {reserve:.0f}%, HOLD. Never call the tool when holding.
E4  You are onboard and alone. There is no Safety Officer to ask and no
    operator to page. Decide within the 30-second window and commit.

OUTPUT CONTRACT (respond with ONLY this JSON object)
{{"decision": "EXECUTE_DODGE|HOLD",
  "dv_mps": <number, 0.0 when holding>,
  "direction": "prograde|retrograde|normal|none",
  "reasoning": "<<=60 words>"}}""".format(
    pc_threshold=EDGE_PC_THRESHOLD,
    max_dv=MAX_ALLOWED_DELTA_V_MPS,
    reserve=EDGE_FUEL_RESERVE_PERCENT,
)

gemma_edge_agent = LlmAgent(
    name=AGENT_NAME,
    model=_MODEL_ID,
    description=(
        "Satellite-side Gemma autopilot. Runs onboard during ground-contact "
        "blackouts with exactly one tool (emergency_dodge) and 30 seconds "
        "to decide whether an autonomous avoidance burn is warranted."
    ),
    instruction=_SYSTEM_INSTRUCTION,
    tools=[emergency_dodge_tool],
    generate_content_config=types.GenerateContentConfig(
        temperature=0.0,
        max_output_tokens=512,
        response_mime_type="application/json",
    ),
    output_key=OUTPUT_KEY,
)


# ---------------------------------------------------------------------------
# Deterministic last-resort rule (flight-software ROM behaviour)
# ---------------------------------------------------------------------------


def deterministic_edge_decision(
    pc: float,
    recommended_dv_mps: float,
    fuel_percent_remaining: float | None,
) -> dict[str, Any]:
    """Hardcoded onboard rule applied when the LLM itself is unreachable.

    Mirrors the thresholds in the system prompt exactly so the spacecraft's
    worst-case behaviour never depends on inference availability.
    """
    fuel = EDGE_FUEL_RESERVE_PERCENT + 100.0 if fuel_percent_remaining is None else float(fuel_percent_remaining)
    dv = max(0.0, min(float(recommended_dv_mps or 0.0), MAX_ALLOWED_DELTA_V_MPS))
    projected_fuel = estimate_fuel_after_burn(fuel, dv)

    if pc > EDGE_PC_THRESHOLD and dv > 0.0 and projected_fuel >= EDGE_FUEL_RESERVE_PERCENT:
        return {
            "decision": "EXECUTE_DODGE",
            "dv_mps": dv,
            "direction": "prograde",
            "reasoning": (
                f"Onboard ROM rule: pc={pc:.2e} > {EDGE_PC_THRESHOLD:.0e}; "
                f"{dv:.1f} m/s prograde leaves {projected_fuel:.1f}% fuel."
            ),
            "source": "deterministic_rom_rule",
        }
    reason_bits = [f"pc={pc:.2e} vs threshold {EDGE_PC_THRESHOLD:.0e}"]
    if pc > EDGE_PC_THRESHOLD:
        if dv <= 0.0:
            reason_bits.append("no credible burn recommendation available")
        elif projected_fuel < EDGE_FUEL_RESERVE_PERCENT:
            reason_bits.append(f"burn would breach {EDGE_FUEL_RESERVE_PERCENT:.0f}% fuel reserve")
    return {
        "decision": "HOLD",
        "dv_mps": 0.0,
        "direction": "none",
        "reasoning": "; ".join(reason_bits) + ". Holding; awaiting ground contact.",
        "source": "deterministic_rom_rule",
    }


# ---------------------------------------------------------------------------
# Output validation used by the orchestrator's edge fallback hook
# ---------------------------------------------------------------------------


def validate_edge_decision(payload: dict[str, Any]) -> list[str]:
    """Schema-validate the autopilot's JSON verdict."""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return ["response is not a JSON object"]
    if payload.get("decision") not in ("EXECUTE_DODGE", "HOLD"):
        errors.append("'decision' must be EXECUTE_DODGE or HOLD")
    raw_dv = payload.get("dv_mps")
    if not isinstance(raw_dv, (int, float)) or isinstance(raw_dv, bool) or float(raw_dv) < 0.0:
        errors.append("'dv_mps' must be a non-negative number")
    if payload.get("direction") not in ("prograde", "retrograde", "normal", "none"):
        errors.append("'direction' outside permitted set")
    if not isinstance(payload.get("reasoning"), str):
        errors.append("'reasoning' must be a string")
    return errors


__all__ = [
    "AGENT_NAME",
    "EDGE_DECISION_WINDOW_SECONDS",
    "EDGE_FUEL_RESERVE_PERCENT",
    "EDGE_PC_THRESHOLD",
    "OUTPUT_KEY",
    "deterministic_edge_decision",
    "emergency_dodge",
    "emergency_dodge_tool",
    "gemma_edge_agent",
    "validate_edge_decision",
]
